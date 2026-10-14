"""Tier 2 - retrack: an auto-record rule's recording following a program the guide moved
(DESIGN-autoschedule.md §5, the Retrack bullet of §11 and interleavings R5 and R14; built in
dev/changelog/1188).

  - MoveTests: a move takes the SCHEDULED recording's live window, scheduled_* and program_*,
    its jobs and the ledger anchor to the new time and logs RECORDING_RULE_RETRACKED with both
    air times and both windows; a capture wait from the old plan ends; the profile's padding
    is used, a recording with no profile keeps the padding it had, and a start whose padding
    has already begun is now; the nearest target wins, not the earliest; the judge after it
    sees the moved window; a group pick follows a listing on another member, whichever group
    labels it; a target another recording already covers (on the pick's channel or a
    simulcast) is not taken and is said once; a whole evening that slid an hour and two
    episodes that swapped slots each follow, since a displaced recording holds nothing; a
    lower-id rule judged after the owner's retrack does not schedule the moved program again,
    by window or by program time; the second pass sees what an earlier rule just made; a
    second evaluation changes nothing.
  - NameTests: the name the rule gave the recording is rendered again for the new time; a
    name the user changed, or one the row never recorded, is kept.
  - UserEditTests (R5): edited times make the row user_modified, leave the recording exactly
    as the user left it with an event saying why, and the new time is judged on its own -
    moved later it is not a repeat, moved earlier it is scheduled, and a recording the user
    already moved onto it covers it.
  - NoMoveTests: a vanished program, a target the rule already judged, another episode under
    the same title, a pick with no episode, a simulcast still listing the program, an
    episode-less listing of the title at the old time, a pick whose program already aired, and
    a program still listed outside the rule's own matches move nothing.
  - FateTests: a FAILED pick is not followed and the new time is recorded; a cancelled pick's
    row follows and stays cancelled; a started, finished or cancelled-but-present recording
    keeps its window and says why; a pick aborted by a group change is scheduled again at the
    new time.
  - ConcurrencyAndFailureTests: a pause landing mid-run stops the move; a start that cannot be
    armed after a move is said on the recording, its row and the rule, and leaves no job at
    the old slot.
  - ReplayInterleavingTests (R14): a race vanishing as its same-episode replay appears reads
    as a move; the race coming back is judged new and recorded too.

Every test builds on make_test_app() through the ledger suite's fixture, which patches
app.routes.recordings.schedule_recording, so no scheduler job is armed. No network, no ffmpeg.

    python3 -m unittest tests.test_auto_rules_retrack
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.test_auto_rules_ledger import _LedgerCase, _future  # noqa: E402
from app import auto_rules, db  # noqa: E402
from app.alerts import create_alert  # noqa: E402
from app.database import (  # noqa: E402
    Alert, AutoRule, CANCEL_DURING_CAPTURE, CANCEL_GROUP_CHANGE, Channel, EPGEntry,
    RECORDING_RULE_PROGRAM_MOVED, RECORDING_RULE_RETRACKED, RECORDING_START_NOT_ARMED,
    REC_STATUS_ABORTED, REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS,
    REC_STATUS_SCHEDULED, RULE_MATCH_SCHEDULED,
    RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_USER_MODIFIED,
    Recording, RecordingEvent, RecordingProfile,
)
from app.routes import recordings as rec_routes  # noqa: E402

_REAL_LOAD_CONFIG = config_mod.load_config
_SLOT_WAIT = 'RECORDING_WAITING_FOR_CONNECTION_SLOT'


def _template(template):
    """Patch app.config.load_config to answer the real config with the global filename
    template set - the engine reads it at run time, where make_test_app overrides do not
    reach."""
    def fake(*args, **kwargs):
        cfg = copy.deepcopy(_REAL_LOAD_CONFIG(*args, **kwargs))
        cfg.setdefault('recording', {})['filename_template'] = template
        return cfg
    return patch('app.config.load_config', side_effect=fake)


class _RetrackCase(_LedgerCase):

    SEARCH = 'grain=airings&q=Cup+Race'

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.profile = RecordingProfile(name='Race padding', pre_padding_minutes=5,
                                        post_padding_minutes=30)
        db.session.add(self.profile)
        db.session.commit()
        self.t0 = _future(hours=10)

    def race_rule(self, **kw):
        kw.setdefault('max_upcoming', 0)
        kw.setdefault('profile_id', self.profile.id)
        return self.rule(self.SEARCH, name='Cup', **kw)

    def scheduled(self, sub_title='Sonoma', start=None, channel=None, minutes=180, **kw):
        """A race on the guide, a rule for it, and the recording its first check made."""
        start = start or self.t0
        self.entry = self.showing(channel or self.ch, 'Cup Race', start, minutes=minutes,
                                  sub_title=sub_title)
        db.session.commit()
        rid = self.race_rule(**kw)
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1, out)
        recs = self.recordings(rid)
        self.assertEqual(len(recs), 1)
        return rid, recs[0]

    def move(self, entry, start, minutes=180):
        """What an import does to a program that moved: its listing is deleted and a new one
        is written at the new time on the same channel - a new id, never an update."""
        old = db.session.get(EPGEntry, entry.id)
        ch = db.session.get(Channel, old.channel_id)
        title, sub = old.title, old.sub_title
        db.session.delete(old)
        new = self.showing(ch, title, start, minutes=minutes, sub_title=sub)
        db.session.commit()
        return new

    def events(self, rec_id, event_type):
        return (RecordingEvent.query.filter_by(recording_id=rec_id, event_type=event_type)
                .order_by(RecordingEvent.id).all())

    def rec(self, rec_id):
        db.session.expire_all()
        return db.session.get(Recording, rec_id)

    def window(self, rec):
        return (rec.start_time, rec.stop_time, rec.scheduled_start_time,
                rec.scheduled_stop_time, rec.program_start_time, rec.program_stop_time,
                rec.name)

    def user_edit(self, rec, start, stop, name=None):
        """A person saving the edit dialog: the real edit path, which re-baselines
        scheduled_* and logs RECORDING_EDITED."""
        rec_routes._apply_edit_and_reschedule(rec.id, name or rec.name, rec.url, start, stop)
        db.session.expire_all()


# ---------------------------------------------------------------------------
# A move
# ---------------------------------------------------------------------------

class MoveTests(_RetrackCase):

    def test_a_move_takes_the_window_the_program_times_the_jobs_and_the_anchor_along(self):
        """§5, §11: the live window is the new air time plus the profile's padding,
        scheduled_* are re-baselined, program_* move, the start is re-armed at the new
        window, the anchor and rec_* follow, and the event keeps both air times and both
        windows - the only record of where the program was."""
        rid, rec = self.scheduled()
        old_window = (rec.start_time, rec.stop_time)
        self.assertEqual(old_window, (self.t0 - timedelta(minutes=5),
                                      self.t0 + timedelta(minutes=210)))
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)
        self.armed.reset_mock()

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual(out['already_scheduled'], 0,
                         'a moved recording is counted as moved, never also as already scheduled')
        recs = self.recordings(rid)
        self.assertEqual([r.id for r in recs], [rec.id], 'followed, never scheduled twice')
        rec = recs[0]
        start, stop = new_air - timedelta(minutes=5), new_air + timedelta(minutes=210)
        self.assertEqual((rec.start_time, rec.stop_time), (start, stop))
        self.assertEqual((rec.scheduled_start_time, rec.scheduled_stop_time), (start, stop))
        self.assertEqual((rec.program_start_time, rec.program_stop_time),
                         (new_air, new_air + timedelta(minutes=180)))
        self.assertEqual(rec.status, REC_STATUS_SCHEDULED)
        self.assertEqual([c.args[1:] for c in self.armed.call_args_list],
                         [(rec.id, start, stop)])
        self.assertIn(rec.id, [c.args[0] for c in self.unscheduled_by_cancel.call_args_list],
                      "the old window's jobs come off before the new ones go on")

        rows = self.ledger(rid)
        self.assertEqual(len(rows), 1, 'the row moved; no second row at either time')
        row = rows[0]
        self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(row.recording_id, rec.id)
        self.assertEqual((row.program_start_time, row.program_stop_time),
                         (new_air, new_air + timedelta(minutes=180)))
        self.assertEqual((row.rec_start_time, row.rec_stop_time), (start, stop))
        self.assertIn('Moved with the program', row.detail)

        events = self.events(rec.id, RECORDING_RULE_RETRACKED)
        self.assertEqual(len(events), 1)
        extra = json.loads(events[0].extra_data)
        self.assertEqual(extra['old_program_start'], self.t0.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(extra['new_program_start'], new_air.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(extra['old_start'], old_window[0].strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(extra['new_stop'], stop.strftime('%Y-%m-%dT%H:%M:%S'))
        self.assertEqual(extra['rule_id'], rid)
        self.assertIn('moved this recording with its program', events[0].detail)

    def test_a_second_evaluation_after_a_move_changes_nothing(self):
        rid, rec = self.scheduled()
        self.move(self.entry, self.t0 + timedelta(hours=2))
        self.evaluate(rid)
        before = (self.snapshot(rid), self.window(self.rec(rec.id)))
        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual((self.snapshot(rid), self.window(self.rec(rec.id))), before)
        self.assertEqual(len(self.events(rec.id, RECORDING_RULE_RETRACKED)), 1)

    def test_a_move_ends_a_capture_wait_from_the_old_plan(self):
        """§5: a recording already held behind a connection slot is waiting on the old plan;
        the hold and its standing alert come down with the move (dev/changelog/1141)."""
        rid, rec = self.scheduled()
        r = self.rec(rec.id)
        r.start_deferred_since = datetime.utcnow() - timedelta(minutes=3)
        r.start_deferred_for = 'a connection slot on Acct'
        db.session.commit()
        create_alert(_SLOT_WAIT, f'Recording waiting for a connection slot: {r.name}',
                     body='waiting', source='account:1:slot-wait', recording_id=rec.id)
        self.move(self.entry, self.t0 + timedelta(hours=2))

        self.evaluate(rid)
        r = self.rec(rec.id)
        self.assertIsNone(r.start_deferred_since)
        self.assertIsNone(r.start_deferred_for)
        self.assertEqual(Alert.query.filter_by(alert_type=_SLOT_WAIT, recording_id=rec.id,
                                               dismissed_at=None).count(), 0)

    def test_a_recording_with_no_profile_keeps_the_padding_it_had(self):
        """A deleted profile unlinks its recordings; the move keeps the padding the recording
        was scheduled with rather than dropping it to none."""
        rid, rec = self.scheduled()
        resp = self.t.client.delete(f'/api/profiles/{self.profile.id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        self.assertIsNone(self.rec(rec.id).profile_id)
        new_air = self.t0 + timedelta(hours=3)
        self.move(self.entry, new_air)

        self.evaluate(rid)
        r = self.rec(rec.id)
        self.assertEqual((r.start_time, r.stop_time),
                         (new_air - timedelta(minutes=5), new_air + timedelta(minutes=210)))

    def test_the_profiles_padding_as_it_stands_is_used(self):
        rid, rec = self.scheduled()
        p = db.session.get(RecordingProfile, self.profile.id)
        p.pre_padding_minutes, p.post_padding_minutes = 15, 45
        db.session.commit()
        new_air = self.t0 + timedelta(hours=3)
        self.move(self.entry, new_air)

        self.evaluate(rid)
        r = self.rec(rec.id)
        self.assertEqual((r.start_time, r.stop_time),
                         (new_air - timedelta(minutes=15), new_air + timedelta(minutes=225)))

    def test_a_start_whose_padding_has_already_begun_is_now(self):
        p = db.session.get(RecordingProfile, self.profile.id)
        p.pre_padding_minutes = 120
        db.session.commit()
        rid, rec = self.scheduled()
        new_air = _future(minutes=30)
        self.move(self.entry, new_air)

        before = datetime.utcnow()
        self.evaluate(rid)
        after = datetime.utcnow()
        r = self.rec(rec.id)
        self.assertTrue(before <= r.start_time <= after, (before, r.start_time, after))
        self.assertEqual(r.scheduled_start_time, r.start_time)
        self.assertEqual(r.program_start_time, new_air)

    def test_the_nearest_target_wins_not_the_earliest(self):
        """The earlier candidate is farther from the old start than the later one, so the
        recording follows the later; the earlier is judged on its own (an earlier airing is
        never a repeat, so it is recorded too)."""
        rid, rec = self.scheduled()
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        earlier, near = self.t0 - timedelta(hours=3), self.t0 + timedelta(hours=1)
        self.showing(self.ch, 'Cup Race', earlier, minutes=180, sub_title='Sonoma')
        self.showing(self.ch, 'Cup Race', near, minutes=180, sub_title='Sonoma')
        db.session.commit()

        self.evaluate(rid)
        self.assertEqual(self.rec(rec.id).program_start_time, near)
        self.assertEqual(self.row_at(rid, near).recording_id, rec.id)
        self.assertEqual(self.row_at(rid, earlier).action_taken, RULE_MATCH_SCHEDULED)
        self.assertNotEqual(self.row_at(rid, earlier).recording_id, rec.id)

    def test_a_farther_target_is_a_repeat_of_the_one_followed(self):
        rid, rec = self.scheduled()
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        near, far = self.t0 + timedelta(hours=1), self.t0 + timedelta(hours=26)
        self.showing(self.ch, 'Cup Race', far, minutes=180, sub_title='Sonoma')
        self.showing(self.ch, 'Cup Race', near, minutes=180, sub_title='Sonoma')
        db.session.commit()

        self.evaluate(rid)
        self.assertEqual(self.rec(rec.id).program_start_time, near)
        self.assertEqual([r.id for r in self.recordings(rid)], [rec.id])
        self.assertEqual(self.row_at(rid, far).action_taken, RULE_MATCH_SKIPPED_REPEAT)

    def test_a_program_the_move_uncovered_is_judged_again_against_the_new_window(self):
        """The judge after retrack sees the moved recording at its NEW window: a short
        program the old window covered (skipped_existing) is no longer covered and is
        scheduled, rather than lost behind a window that left."""
        rid, rec = self.scheduled()
        short = self.t0 + timedelta(minutes=30)
        self.showing(self.ch, 'Cup Race', short, minutes=60, sub_title='Watkins Glen')
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, short).action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.move(self.entry, self.t0 + timedelta(hours=5))

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1)
        self.assertEqual(out['scheduled'], 1, out)
        row = self.row_at(rid, short)
        self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
        self.assertNotEqual(row.recording_id, rec.id)

    def test_a_group_pick_follows_a_listing_on_another_member(self):
        """A group pick's channel is the member it was planned on; a listing on any member of
        its group is the group."""
        east, west = self.channel('Cup East'), self.channel('Cup West', health_score=10)
        group = seed.make_group(name='Cup Feeds', members=[east, west])
        db.session.commit()
        entries = [self.showing(ch, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
                   for ch in (east, west)]
        db.session.commit()
        rid = self.race_rule()
        self.evaluate(rid)
        rec = self.recordings(rid)[0]
        self.assertEqual(rec.group_id, group.id)
        other = west if rec.channel_id == east.id else east
        for e in entries:
            db.session.delete(db.session.get(EPGEntry, e.id))
        new_air = self.t0 + timedelta(hours=2)
        self.showing(other, 'Cup Race', new_air, minutes=180, sub_title='Sonoma')
        db.session.commit()

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1)
        r = self.rec(rec.id)
        self.assertEqual(r.program_start_time, new_air)
        self.assertEqual(r.group_id, group.id)
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_group_pick_follows_a_member_whose_listing_another_group_labels(self):
        """The new listing's row can be labeled with a different group the member also
        belongs to (one in the guide, or larger); membership is what makes it the pick's
        group, so the recording still follows - and stays on its own group."""
        east, west = self.channel('Cup East'), self.channel('Cup West', health_score=10)
        group = seed.make_group(name='Cup Feeds', members=[east, west])
        db.session.commit()
        entries = [self.showing(ch, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
                   for ch in (east, west)]
        db.session.commit()
        rid = self.race_rule()
        self.evaluate(rid)
        rec = self.recordings(rid)[0]
        self.assertEqual(rec.group_id, group.id)
        other = west if rec.channel_id == east.id else east
        bigger = seed.make_group(name='All Racing', members=[
            other, self.channel('Racing Two'), self.channel('Racing Three')])
        for e in entries:
            db.session.delete(db.session.get(EPGEntry, e.id))
        new_air = self.t0 + timedelta(hours=2)
        self.showing(other, 'Cup Race', new_air, minutes=180, sub_title='Sonoma')
        db.session.commit()

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1, out)
        r = self.rec(rec.id)
        self.assertEqual(r.program_start_time, new_air)
        self.assertEqual(r.group_id, group.id)
        self.assertNotEqual(r.group_id, bigger.id)
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_target_another_recording_already_covers_is_not_taken(self):
        """A person has already scheduled a block over the program's new time. Moving the
        rule's recording there too would make the second recording of one program that
        step 4's existing check exists to prevent; the new time is skipped as covered and the
        rule's recording keeps its slot, as for a program that vanished."""
        rid, rec = self.scheduled()
        before = self.window(rec)
        new_air = self.t0 + timedelta(hours=4)
        block = self.library('Speed afternoon', new_air - timedelta(hours=1),
                             status=REC_STATUS_SCHEDULED, channel=self.ch,
                             start=new_air - timedelta(hours=1),
                             stop=new_air + timedelta(hours=5))
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual(self.window(self.rec(rec.id)), before)
        self.assertEqual(self.events(rec.id, RECORDING_RULE_RETRACKED), [])
        row = self.row_at(rid, new_air)
        self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertIn(str(block.id), row.detail)
        self.assertEqual(Recording.query.filter(
            Recording.program_start_time == new_air).count(), 0)
        events = self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)
        self.assertEqual(len(events), 1, 'the pick left behind says where the program went')
        self.assertEqual(json.loads(events[0].extra_data)['reason'], 'covered')
        self.assertEqual(json.loads(events[0].extra_data)['covered_by'], block.id)
        self.assertIn('records it there', self.row_at(rid, self.t0).detail)

        self.evaluate(rid)
        self.assertEqual(len(self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)), 1,
                         'said once, not on every check')

    def test_a_block_on_a_simulcast_channel_covering_the_new_time_holds_it(self):
        """The holder test is step 4's: a recording covering ANY showing of the program at its
        new time, not only the one on the pick's own channel."""
        self.ch.health_score = 100
        simulcast = self.channel('Simulcast', health_score=10)
        db.session.commit()
        twin = self.showing(simulcast, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
        rid, rec = self.scheduled()
        self.assertEqual(rec.channel_id, self.ch.id)
        before = self.window(rec)
        new_air = self.t0 + timedelta(hours=4)
        block = self.library('Simulcast evening', new_air - timedelta(hours=1),
                             status=REC_STATUS_SCHEDULED, channel=simulcast,
                             start=new_air - timedelta(hours=1),
                             stop=new_air + timedelta(hours=5))
        self.move(self.entry, new_air)
        self.move(twin, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self.assertEqual(self.window(self.rec(rec.id)), before)
        self.assertEqual(self.row_at(rid, new_air).action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertEqual(json.loads(self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)[0]
                                    .extra_data)['covered_by'], block.id)

    def test_a_whole_evening_that_slid_an_hour_follows_episode_by_episode(self):
        """Back-to-back picks shifted together: the second's old window covers the first's
        new time, but it is about to move itself, so it holds nothing."""
        rid, first = self.scheduled(minutes=60)
        second_entry = self.showing(self.ch, 'Cup Race', self.t0 + timedelta(hours=1),
                                    minutes=60, sub_title='Watkins Glen')
        db.session.commit()
        self.evaluate(rid)
        second = next(r for r in self.recordings(rid) if r.id != first.id)
        self.move(second_entry, self.t0 + timedelta(hours=2), minutes=60)
        self.move(self.entry, self.t0 + timedelta(hours=1), minutes=60)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 2, out)
        self.assertEqual(self.rec(first.id).program_start_time, self.t0 + timedelta(hours=1))
        self.assertEqual(self.rec(second.id).program_start_time, self.t0 + timedelta(hours=2))
        self.assertEqual(len(self.recordings(rid)), 2)
        self.assertEqual({m.action_taken for m in self.ledger(rid)}, {RULE_MATCH_SCHEDULED})

    def test_two_episodes_that_swapped_slots_each_follow_their_own(self):
        rid, first = self.scheduled(minutes=60)
        later = self.t0 + timedelta(hours=3)
        second_entry = self.showing(self.ch, 'Cup Race', later, minutes=60,
                                    sub_title='Watkins Glen')
        db.session.commit()
        self.evaluate(rid)
        second = next(r for r in self.recordings(rid) if r.id != first.id)
        self.move(self.entry, later, minutes=60)
        self.move(second_entry, self.t0, minutes=60)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 2, out)
        self.assertEqual(self.rec(first.id).program_start_time, later)
        self.assertEqual(self.rec(second.id).program_start_time, self.t0)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_a_rule_judged_before_the_owner_retracks_does_not_schedule_the_moved_program(self):
        """Rules are evaluated in id order. Every rule's retrack lands before any rule judges,
        so a lower-id rule that also matches the program finds the owner's recording already
        at the new time instead of scheduling a second one."""
        first = self.rule('grain=airings&q=Cup', name='All Cup', skip_repeats=False,
                          max_upcoming=0)
        auto_rules.set_rule_enabled(first, False)
        rid, rec = self.scheduled()
        self.assertGreater(rid, first)
        auto_rules.set_rule_enabled(first, True)
        self.evaluate(first)
        self.assertEqual(self.row_at(first, self.t0).action_taken, RULE_MATCH_SKIPPED_EXISTING)
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)

        out = self.sweep()
        self.assertEqual(out[rid].retracked, 1)
        self.assertEqual(out[first].scheduled, 0)
        at_new = Recording.query.filter(Recording.program_start_time == new_air).all()
        self.assertEqual([r.id for r in at_new], [rec.id])
        self.assertEqual(self.row_at(first, new_air).action_taken, RULE_MATCH_SKIPPED_EXISTING)

    def test_a_lower_id_rule_on_a_simulcast_finds_the_moved_program_by_what_it_is(self):
        """The lower-id rule only searches the simulcast channel, so no window of the owner's
        recording covers its showing: only the moved recording's program time, as the judge
        reads it after retrack, tells it the program is already recorded."""
        self.ch.health_score = 100
        simulcast = self.channel('Simulcast', health_score=10)
        db.session.commit()
        twin = self.showing(simulcast, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
        first = self.rule(f'grain=airings&q=Cup&f.chan={simulcast.id}', name='Simulcast Cup',
                          skip_repeats=False, max_upcoming=0)
        auto_rules.set_rule_enabled(first, False)
        rid, rec = self.scheduled()
        self.assertEqual(rec.channel_id, self.ch.id)
        auto_rules.set_rule_enabled(first, True)
        self.evaluate(first)
        self.assertEqual(self.row_at(first, self.t0).action_taken, RULE_MATCH_SKIPPED_EXISTING)
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)
        self.move(twin, new_air)

        out = self.sweep()
        self.assertEqual(out[rid].retracked, 1)
        self.assertEqual(out[first].scheduled, 0, out[first])
        at_new = Recording.query.filter(Recording.program_start_time == new_air).all()
        self.assertEqual([r.id for r in at_new], [rec.id])

    def test_the_second_pass_sees_what_an_earlier_rule_just_made(self):
        """Two rules, two channels, one race: one listing carries the episode, the other
        does not. The episode the first rule's new recording carries makes the second rule's
        listing the same program (§3.3), exactly as a single pass would have read it."""
        sky, espn = self.channel('Sky'), self.channel('Espn')
        db.session.commit()
        self.showing(sky, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
        self.showing(espn, 'Cup Race', self.t0, minutes=180)
        db.session.commit()
        a = self.rule(f'grain=airings&q=Cup+Race&f.chan={sky.id}', name='Sky', max_upcoming=0)
        b = self.rule(f'grain=airings&q=Cup+Race&f.chan={espn.id}', name='Espn',
                      max_upcoming=0)
        self.assertLess(a, b)

        out = self.sweep()
        self.assertEqual(out[a].scheduled, 1)
        self.assertEqual(out[b].scheduled, 0, out[b])
        self.assertEqual(Recording.query.filter(
            Recording.program_start_time == self.t0).count(), 1)
        self.assertEqual(self.row_at(b, self.t0).action_taken, RULE_MATCH_SKIPPED_EXISTING)


# ---------------------------------------------------------------------------
# The name
# ---------------------------------------------------------------------------

class NameTests(_RetrackCase):

    TEMPLATE = '{start_time} {title}'

    def _clock(self, dt):
        from app.tz_utils import get_display_tz, to_local
        return to_local(dt, get_display_tz()).strftime('%H%M')

    def test_the_rules_name_follows_the_program(self):
        with _template(self.TEMPLATE):
            rid, rec = self.scheduled()
            self.assertEqual(rec.name, f'{self._clock(self.t0)} Cup Race')
            new_air = self.t0 + timedelta(hours=2)
            self.move(self.entry, new_air)
            self.evaluate(rid)
            want = f'{self._clock(new_air)} Cup Race'
        r = self.rec(rec.id)
        self.assertEqual(r.name, want)
        row = self.ledger(rid)[0]
        self.assertEqual(row.rec_name, want, 'the row remembers the name it gave')
        event = self.events(rec.id, RECORDING_RULE_RETRACKED)[0]
        self.assertIn(want, event.detail)

    def test_a_name_the_user_changed_is_kept_and_the_times_still_move(self):
        with _template(self.TEMPLATE):
            rid, rec = self.scheduled()
            self.user_edit(rec, rec.start_time, rec.stop_time, name='My race')
            new_air = self.t0 + timedelta(hours=2)
            self.move(self.entry, new_air)
            out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1, 'a renamed recording still follows its program')
        r = self.rec(rec.id)
        self.assertEqual(r.name, 'My race')
        self.assertEqual(r.program_start_time, new_air)

    def test_a_row_that_never_recorded_a_name_never_renames(self):
        """A row written before rule_matches.rec_name existed carries NULL: nothing says the
        name is still the rule's, so it is left alone."""
        with _template(self.TEMPLATE):
            rid, rec = self.scheduled()
            row = self.ledger(rid)[0]
            row.rec_name = None
            db.session.commit()
            old_name = rec.name
            self.move(self.entry, self.t0 + timedelta(hours=2))
            self.evaluate(rid)
        r = self.rec(rec.id)
        self.assertEqual(r.name, old_name)
        self.assertEqual(r.program_start_time, self.t0 + timedelta(hours=2))


# ---------------------------------------------------------------------------
# R5 - the user edited the recording's times
# ---------------------------------------------------------------------------

class UserEditTests(_RetrackCase):

    def _edited(self):
        rid, rec = self.scheduled()
        self.user_edit(rec, rec.start_time, rec.stop_time + timedelta(minutes=30))
        return rid, self.rec(rec.id)

    def _assert_let_go(self, rid, rec, before):
        r = self.rec(rec.id)
        self.assertEqual(self.window(r), before, 'the edited recording is never touched')
        row = self.row_at(rid, self.t0)
        self.assertEqual(row.action_taken, RULE_MATCH_USER_MODIFIED)
        self.assertEqual(row.recording_id, rec.id)
        self.assertIn('had been edited', row.detail)
        events = self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)
        self.assertEqual(len(events), 1)
        self.assertIn('had been edited', events[0].detail)
        self.assertEqual(self.events(rec.id, RECORDING_RULE_RETRACKED), [])

    def test_moved_later_the_new_time_is_recorded_and_is_not_a_repeat(self):
        rid, rec = self._edited()
        before = self.window(rec)
        new_air = self.t0 + timedelta(hours=5)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self.assertEqual(out['scheduled'], 1, out)
        self._assert_let_go(rid, rec, before)
        new = [r for r in self.recordings(rid) if r.id != rec.id]
        self.assertEqual([r.program_start_time for r in new], [new_air])
        self.assertEqual(self.row_at(rid, new_air).action_taken, RULE_MATCH_SCHEDULED)

    def test_moved_earlier_the_new_time_is_recorded(self):
        rid, rec = self._edited()
        before = self.window(rec)
        new_air = self.t0 - timedelta(hours=5)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1, out)
        self._assert_let_go(rid, rec, before)
        self.assertEqual(self.row_at(rid, new_air).action_taken, RULE_MATCH_SCHEDULED)

    def test_a_recording_the_user_already_moved_onto_the_new_time_covers_it(self):
        rid, rec = self.scheduled()
        new_air = self.t0 + timedelta(hours=4)
        self.user_edit(rec, new_air - timedelta(minutes=10), new_air + timedelta(hours=4))
        rec = self.rec(rec.id)
        before = self.window(rec)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0, out)
        self._assert_let_go(rid, rec, before)
        self.assertEqual([r.id for r in self.recordings(rid)], [rec.id])
        self.assertEqual(self.row_at(rid, new_air).action_taken, RULE_MATCH_SKIPPED_EXISTING)

    def test_the_let_go_recording_is_never_touched_again(self):
        rid, rec = self._edited()
        self.move(self.entry, self.t0 + timedelta(hours=5))
        self.evaluate(rid)
        before = (self.window(self.rec(rec.id)), self.row_at(rid, self.t0).updated_at)
        self.evaluate(rid)
        self.assertEqual((self.window(self.rec(rec.id)), self.row_at(rid, self.t0).updated_at),
                         before)
        self.assertEqual(len(self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)), 1)


# ---------------------------------------------------------------------------
# What never moves
# ---------------------------------------------------------------------------

class NoMoveTests(_RetrackCase):

    def _assert_untouched(self, rid, rec, before_window, before_ledger=None):
        self.assertEqual(self.window(self.rec(rec.id)), before_window)
        if before_ledger is not None:
            self.assertEqual(self.snapshot(rid), before_ledger)
        self.assertEqual(self.events(rec.id, RECORDING_RULE_RETRACKED), [])
        self.assertEqual(self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED), [])

    def test_a_vanished_program_touches_nothing(self):
        rid, rec = self.scheduled()
        before = (self.window(rec), self.snapshot(rid))
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        db.session.commit()
        self.armed.reset_mock()

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self._assert_untouched(rid, rec, *before)
        self.armed.assert_not_called()

    def test_a_replay_the_rule_already_judged_is_never_a_target(self):
        rid, rec = self.scheduled()
        replay = self.t0 + timedelta(days=3)
        self.showing(self.ch, 'Cup Race', replay, minutes=180, sub_title='Sonoma')
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, replay).action_taken, RULE_MATCH_SKIPPED_REPEAT)
        before = (self.window(self.rec(rec.id)), self.snapshot(rid))
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        db.session.commit()

        out = self.evaluate(rid)
        self._assert_untouched(rid, rec, *before)
        # Clean, not merely rolled back: taking a judged anchor would collide with the
        # replay's own row and stop the whole check.
        self.assertIsNone(out['error'])
        self.assertIsNone(db.session.get(AutoRule, rid).last_error)
        self.assertIsNone(db.session.get(AutoRule, rid).last_problems)

    def test_another_episode_under_the_same_title_is_never_a_target(self):
        rid, rec = self.scheduled()
        before = self.window(rec)
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        other = self.t0 + timedelta(hours=2)
        self.showing(self.ch, 'Cup Race', other, minutes=180, sub_title='Watkins Glen')
        db.session.commit()

        out = self.evaluate(rid)
        self._assert_untouched(rid, rec, before)
        self.assertEqual(out['scheduled'], 1, 'the other race is a program of its own')
        self.assertEqual(self.row_at(rid, other).action_taken, RULE_MATCH_SCHEDULED)

    def test_a_pick_with_no_episode_never_moves(self):
        """§5: under a bare series title a moved race cannot be told from next week's or a
        replay, so the recording keeps its slot and the new time is judged on its own - a
        spare recording rather than a lost race."""
        rid, rec = self.scheduled(sub_title=None)
        before = self.window(rec)
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self._assert_untouched(rid, rec, before)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(sorted(r.program_start_time for r in self.recordings(rid)),
                         [self.t0, new_air])

    def test_a_simulcast_still_listing_the_program_moves_nothing(self):
        """The anchor is channel-free: the scheduled channel's listing gone while another
        channel still lists the program at its time is not a move."""
        self.ch.health_score = 100
        simulcast = self.channel('Simulcast', health_score=10)
        db.session.commit()
        self.showing(simulcast, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
        rid, rec = self.scheduled()
        self.assertEqual(rec.channel_id, self.ch.id)
        before = self.window(rec)
        later = self.t0 + timedelta(hours=2)
        self.move(self.entry, later)

        self.evaluate(rid)
        self._assert_untouched(rid, rec, before)
        self.assertEqual(self.row_at(rid, later).action_taken, RULE_MATCH_SKIPPED_REPEAT)

    def test_an_episode_less_listing_at_the_old_time_counts_as_still_listed(self):
        """Doubt never moves a recording: a listing of the same title with no episode at the
        pick's time, on a channel outside the rule's search, may be the same program."""
        elsewhere = self.channel('Elsewhere')
        self.showing(elsewhere, 'Cup Race', self.t0, minutes=180)
        db.session.commit()
        self.SEARCH = f'grain=airings&q=Cup+Race&f.chan={self.ch.id}'
        rid, rec = self.scheduled()
        before = self.window(rec)
        self.move(self.entry, self.t0 + timedelta(hours=2))

        out = self.evaluate(rid)
        self.assertIsNone(out['error'])
        self._assert_untouched(rid, rec, before)

    def test_a_pick_whose_program_has_already_aired_is_never_followed(self):
        """Only a pick whose program has not started is retracked. A finished race's row,
        with its listing pruned, must not chase that race's replay listed days later."""
        rid = self.race_rule()
        aired = _future(hours=-48)
        done = self.library('Cup Race', aired, sub_title='Sonoma', channel=self.ch,
                            minutes=180, rule_id=rid)
        self.ledger_row(rid, RULE_MATCH_SCHEDULED, 'Cup Race', aired, sub_title='Sonoma',
                        recording=done, minutes=180, channel=self.ch)
        replay = _future(hours=20)
        self.showing(self.ch, 'Cup Race', replay, minutes=180, sub_title='Sonoma')
        db.session.commit()

        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, aired).recording_id, done.id)
        self.assertEqual(self.row_at(rid, replay).action_taken, RULE_MATCH_SKIPPED_REPEAT)
        self.assertEqual(self.events(done.id, RECORDING_RULE_PROGRAM_MOVED), [])
        self.assertEqual(self.events(done.id, RECORDING_RULE_RETRACKED), [])

    def test_a_program_still_listed_outside_the_rules_matches_is_not_a_move(self):
        """A rule narrowed by an edit stops matching a race that is still on the guide at its
        time. That is not a move, so the kept recording is not carried off to a same-episode
        replay the narrowed search does match."""
        rid, rec = self.scheduled()
        replay = self.t0 + timedelta(days=3)
        self.showing(self.ch, 'Cup Race', replay, minutes=120, sub_title='Sonoma')
        db.session.commit()
        self.evaluate(rid)
        rule = db.session.get(AutoRule, rid)
        auto_rules.update_rule(rid, auto_rules.read_rule_values(
            {'search': self.SEARCH + '&f.duration=dur:..150'}, existing=rule))
        before = self.window(self.rec(rec.id))

        self.evaluate(rid)
        self._assert_untouched(rid, rec, before)
        self.assertEqual(self.row_at(rid, replay).action_taken, RULE_MATCH_SKIPPED_REPEAT)


# ---------------------------------------------------------------------------
# What happened to the pick
# ---------------------------------------------------------------------------

class FateTests(_RetrackCase):

    def test_a_failed_pick_is_not_followed_and_the_new_time_is_recorded(self):
        rid, rec = self.scheduled()
        self.set_status(rec.id, REC_STATUS_FAILED)
        before = self.window(self.rec(rec.id))
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 0)
        self.assertEqual(out['scheduled'], 1, out)
        self.assertEqual(self.window(self.rec(rec.id)), before)
        self.assertEqual(self.row_at(rid, self.t0).recording_id, rec.id,
                         'the failed pick keeps its own row')
        self.assertEqual(self.row_at(rid, new_air).action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED), [])

    def test_a_cancelled_pick_follows_the_program_and_stays_cancelled(self):
        """R6 across a move: the row's anchor follows, so the new time is not a program
        nobody judged - nothing is scheduled there."""
        rid, rec = self.scheduled()
        self.cancel_json(rec.id)
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0, out)
        self.assertEqual(self.recordings(rid), [])
        rows = self.ledger(rid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].program_start_time, new_air)
        self.assertEqual(rows[0].action_taken, RULE_MATCH_SCHEDULED)
        self.assertIsNone(rows[0].recording_id)
        self.assertIn('removed', rows[0].detail)

    def test_a_started_recording_keeps_its_window_and_says_so(self):
        rid, rec = self.scheduled()
        self.set_status(rec.id, REC_STATUS_IN_PROGRESS)
        before = self.window(self.rec(rec.id))
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)
        self.armed.reset_mock()

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual(self.window(self.rec(rec.id)), before)
        self.assertEqual(self.row_at(rid, new_air).recording_id, rec.id)
        events = self.events(rec.id, RECORDING_RULE_PROGRAM_MOVED)
        self.assertEqual(len(events), 1)
        self.assertIn('already started', events[0].detail)
        self.assertEqual(json.loads(events[0].extra_data)['reason'], 'started')
        self.armed.assert_not_called()

    def test_a_pick_aborted_by_a_group_change_is_scheduled_again_at_the_new_time(self):
        rid, rec = self.scheduled()
        self.set_status(rec.id, REC_STATUS_ABORTED, CANCEL_GROUP_CHANGE)
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1, out)
        row = self.row_at(rid, new_air)
        self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
        self.assertNotEqual(row.recording_id, rec.id)
        self.assertEqual(self.rec(row.recording_id).program_start_time, new_air)
        self.assertEqual(len(self.ledger(rid)), 1)
        self.assert_said_why(rec.id, 'group_change', 'channel group change')

    def assert_said_why(self, rec_id, reason, phrase):
        events = self.events(rec_id, RECORDING_RULE_PROGRAM_MOVED)
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0].extra_data)['reason'], reason)
        self.assertIn(phrase, events[0].detail)

    def _kept(self, status, cancel_reason=None):
        rid, rec = self.scheduled()
        self.set_status(rec.id, status, cancel_reason)
        before = self.window(self.rec(rec.id))
        new_air = self.t0 + timedelta(hours=2)
        self.move(self.entry, new_air)
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0, out)
        self.assertEqual(self.window(self.rec(rec.id)), before)
        self.assertEqual(self.row_at(rid, new_air).recording_id, rec.id)
        return rec

    def test_a_recording_cancelled_but_still_present_stays_cancelled_and_says_so(self):
        rec = self._kept(REC_STATUS_ABORTED, CANCEL_DURING_CAPTURE)
        self.assert_said_why(rec.id, 'cancelled', 'was cancelled')

    def test_a_finished_recording_keeps_its_window_and_says_so(self):
        rec = self._kept(REC_STATUS_COMPLETED)
        self.assert_said_why(rec.id, 'finished', 'has finished')


# ---------------------------------------------------------------------------
# Concurrency and failure
# ---------------------------------------------------------------------------

class ConcurrencyAndFailureTests(_RetrackCase):

    def test_a_pause_landing_mid_run_stops_the_move(self):
        """§4.3, R12: retrack's write claims the rule like every other write unit."""
        rid, rec = self.scheduled()
        before = (self.window(rec), self.snapshot(rid))
        self.move(self.entry, self.t0 + timedelta(hours=2))
        real = auto_rules._still_listed
        paused = []

        def still_listed(rows):
            if not paused:
                paused.append(1)
                auto_rules.set_rule_enabled(rid, False)
            return real(rows)

        with patch('app.auto_rules._still_listed', side_effect=still_listed):
            outcome = auto_rules.run_evaluation(auto_rules.TRIGGER_QUEUED)
        db.session.expire_all()
        self.assertTrue(paused, 'retrack never reached its guide check')
        self.assertTrue(outcome[rid].abandoned)
        self.assertEqual((self.window(self.rec(rec.id)), self.snapshot(rid)), before)
        self.assertEqual(self.events(rec.id, RECORDING_RULE_RETRACKED), [])

    def test_a_start_that_cannot_be_armed_after_a_move_is_said_everywhere(self):
        """And a capture wait from the old plan still ends: the failed re-arm takes the
        fallback path, which must not skip it."""
        rid, rec = self.scheduled()
        r = self.rec(rec.id)
        r.start_deferred_since = datetime.utcnow() - timedelta(minutes=3)
        r.start_deferred_for = 'a connection slot on Acct'
        db.session.commit()
        self.move(self.entry, self.t0 + timedelta(hours=2))
        self.armed.side_effect = RuntimeError('jobstore is locked')

        out = self.evaluate(rid)
        self.assertEqual(out['retracked'], 1)
        self.assertIn(rec.id, [c.args[0] for c in self.unscheduled_by_cancel.call_args_list],
                      'the old jobs came off first, so nothing stays armed at the old slot')
        self.assertIn('could not be armed', out['problems'] or '')
        self.assertEqual(len(self.events(rec.id, RECORDING_START_NOT_ARMED)), 1)
        self.assertIn('could not be armed', self.ledger(rid)[0].detail)
        self.assertIn('could not be armed', db.session.get(AutoRule, rid).last_problems or '')
        self.assertIsNone(self.rec(rec.id).start_deferred_since)


# ---------------------------------------------------------------------------
# R14 - a race vanishing as its replay appears
# ---------------------------------------------------------------------------

class ReplayInterleavingTests(_RetrackCase):

    def test_the_recording_follows_the_replay_and_a_returning_race_is_recorded_too(self):
        rid, rec = self.scheduled()
        replay = self.t0 + timedelta(days=3)
        db.session.delete(db.session.get(EPGEntry, self.entry.id))
        self.showing(self.ch, 'Cup Race', replay, minutes=180, sub_title='Sonoma')
        db.session.commit()

        self.evaluate(rid)
        self.assertEqual(self.rec(rec.id).program_start_time, replay,
                         'indistinguishable from a move, and documented as such')

        self.showing(self.ch, 'Cup Race', self.t0, minutes=180, sub_title='Sonoma')
        db.session.commit()
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1, 'the race is earlier than the replay, so not '
                                              'a repeat of it')
        self.assertEqual(sorted(r.program_start_time for r in self.recordings(rid)),
                         [self.t0, replay])


if __name__ == '__main__':
    unittest.main()
