"""Tier 2 - what an auto-record rule decides about each showing, and what its lifecycle does to
what it decided (DESIGN-autoschedule.md §2, §3.4-§3.6, §4.3 step 4, §8, and the NASCAR,
Repeats and Ledger bullets of §11; built in dev/changelog/1186).

  - ActionTableTests (§2): the six verdicts, which of them an evaluation judges again, and
    that evaluation never deletes a ledger row.
  - NascarScenarioTests (§3.5, §11): the spec's drifting Cup start times. With skip repeats and
    no limit exactly the races are scheduled, once each, on the group - a doubleheader inside
    the previous race's padding included - and every other showing has a row saying why.
    "1 within 168 hours" loses the 08-29 and 09-13 races as documented, "1 within 120 hours"
    keeps all four, and the window is half-open.
  - RepeatTests (§3.4, R6, R8): only an EARLIER airing with the same non-empty episode makes a
    repeat; what counts as already recorded in the library and in the rule's own picks.
  - ExistingCheckTests (§4.3 step 4.2, R1, R2): the same program already recorded anywhere, or
    a same-channel recording covering the whole air time, skips; a padding overlap does not;
    the first recording made wins and cancelling it lets the other take over.
  - LedgerAndCountTests (§2, §3.5, §3.6): idempotence, a cancellation sticking, the safety cap
    and its alert, the limit's counted picks, and an unchanged verdict writing nothing.
  - EditTests, DeleteTests, ProfileDeleteTests, PauseResumeTests (§8).

Every test builds on make_test_app() and patches app.routes.recordings.schedule_recording, so
no scheduler job is ever armed, and the two unschedule_recording bindings, so a delete's
unscheduling can be asserted. No network, no ffmpeg.

    python3 -m unittest tests.test_auto_rules_ledger
"""
import os
import sqlite3
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import auto_rules, db  # noqa: E402
from app.alerts import (AUTO_RULE_BROKEN, AUTO_RULE_CAPPED,  # noqa: E402
                        AUTO_RULES_GUIDE_SHORT, create_alert)
from app.channel_search import MAX_PAGE_SIZE, SearchContext, search  # noqa: E402
from app.channel_search_rows import REC_DEAD, REC_DONE, REC_LIVE  # noqa: E402
from app.database import (  # noqa: E402
    Alert, AutoRule, CANCEL_BEFORE_START, CANCEL_DURING_ANALYSIS, CANCEL_DURING_CAPTURE,
    CANCEL_DURING_CONVERSION, CANCEL_GROUP_CHANGE, RECORDING_RULE_SCHEDULED,
    REC_STATUS_ABORTED, REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_IN_PROGRESS,
    REC_STATUS_SCHEDULED, RULE_MATCH_ACTIONS, RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP,
    RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_LIMIT, RULE_MATCH_SKIPPED_REPEAT,
    RULE_MATCH_SKIPS, RULE_MATCH_USER_MODIFIED, Recording, RecordingEvent, RecordingProfile,
    RuleMatch,
)
from app.program_titles import title_key  # noqa: E402

#: Distinct words for subtests that each need a program of their own - a title per case, so
#: one case's recordings can never be another's existing recording or earlier airing.
_WORDS = ('Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot', 'Golf', 'Hotel', 'India',
          'Juliet', 'Kilo', 'Lima')

#: The person's own cancel reasons (§3.4): an ABORTED pick with one of these still counts.
_BY_A_PERSON = (CANCEL_BEFORE_START, CANCEL_DURING_CAPTURE, CANCEL_DURING_ANALYSIS,
                CANCEL_DURING_CONVERSION)

#: Four real Cup races (UTC), the §3.5/§11 drifting start times: gaps of 148, 190 and 166
#: hours, so "1 within 7 days" (168 hours) suppresses the second and the fourth.
CUP_RACES = (datetime(2026, 8, 23, 19), datetime(2026, 8, 29, 23), datetime(2026, 9, 6, 21),
             datetime(2026, 9, 13, 19))
CUP_EPISODES = ('Race at Bristol', 'Race at Richmond', 'Race at Darlington', 'Race at Gateway')
_WEEK = timedelta(days=7)


def _future(hours=0, minutes=0):
    """A minute-aligned naive-UTC moment ahead of now - two showings that must share an exact
    start take the same value rather than two separate offsets from the clock."""
    return (datetime.utcnow() + timedelta(hours=hours, minutes=minutes)).replace(
        second=0, microsecond=0)


def _season(now):
    """CUP_RACES moved forward by whole weeks until the first is at least three days ahead.
    Whole weeks keep every gap exactly as it was, which is what the limit tests measure."""
    weeks = 0
    while CUP_RACES[0] + weeks * _WEEK < now + timedelta(days=3):
        weeks += 1
    return [start + weeks * _WEEK for start in CUP_RACES]


# duplicated from tests/test_auto_rules_matching.py::_RuleCase (setUp and the channel,
# showing, rule and evaluate helpers) - each auto-rule test module owns its fixture so either
# can change without breaking the other; this one adds the library, ledger and cancel helpers.
class _LedgerCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.armed = patch('app.routes.recordings.schedule_recording').start()
        # delete_rule() imports unschedule_recording at call time; the cancel routes hold
        # their own module-level binding.
        self.unscheduled = patch('app.scheduler.unschedule_recording').start()
        self.unscheduled_by_cancel = patch('app.routes.recordings.unschedule_recording').start()
        self.addCleanup(patch.stopall)
        self.acc = seed.make_account()
        self._next_stream = 100

    def tearDown(self):
        self.t.cleanup()

    # -- seeding ---------------------------------------------------------------------------

    def channel(self, name, **kw):
        # Stream ids unique across the test, so no two channels share a stream URL and the
        # duplicate-stream fold never merges them.
        self._next_stream += 1
        return seed.make_channel(self.acc, stream_id=self._next_stream, name=name, **kw)

    def showing(self, channel, title, start, minutes=60, sub_title=None):
        return seed.make_epg_entry(channel, title=title, sub_title=sub_title,
                                   start_time=start, duration_minutes=minutes)

    def library(self, title, program_start, status=REC_STATUS_COMPLETED, sub_title=None,
                channel=None, minutes=60, start=None, stop=None, **kw):
        """A recording already in the library, with the program snapshot the create path
        takes. Its window defaults to the air time itself."""
        program_stop = program_start + timedelta(minutes=minutes)
        rec = seed.make_recording(
            status=status, name=kw.pop('name', f'{title} ({status.lower()})'),
            channel_id=channel.id if channel is not None else None,
            start_time=start or program_start, stop_time=stop or program_stop,
            program_title=title, program_sub_title=sub_title,
            program_start_time=program_start, program_stop_time=program_stop, **kw)
        db.session.commit()
        return rec

    def ledger_row(self, rule_id, action, title, start, sub_title=None, recording=None,
                   minutes=60, channel=None):
        """A ledger row written straight into the table, for states an evaluation reaches
        only through retrack or long ago."""
        m = RuleMatch(rule_id=rule_id, title_key=title_key(title),
                      episode_key=title_key(sub_title), program_title=title,
                      program_sub_title=sub_title, program_start_time=start,
                      program_stop_time=start + timedelta(minutes=minutes),
                      action_taken=action, channel_id=channel.id if channel else None,
                      recording_id=recording.id if recording is not None else None,
                      rec_start_time=recording.start_time if recording is not None else None,
                      rec_stop_time=recording.stop_time if recording is not None else None,
                      detail='seeded', created_at=datetime.utcnow(),
                      updated_at=datetime.utcnow())
        db.session.add(m)
        db.session.commit()
        return m

    def rule(self, search, **kw):
        data = {'name': kw.pop('name', 'Test rule'), 'search': search}
        data.update(kw)
        return auto_rules.create_rule(auto_rules.read_rule_values(data))

    # -- acting ----------------------------------------------------------------------------

    def evaluate(self, rule_id):
        out = auto_rules.evaluate_now(rule_id)
        db.session.expire_all()
        return out

    def sweep(self):
        out = auto_rules.run_evaluation(auto_rules.TRIGGER_SWEEP)
        db.session.expire_all()
        return out

    def set_status(self, recording_id, status, cancel_reason=None):
        rec = db.session.get(Recording, recording_id)
        rec.status = status
        rec.cancel_reason = cancel_reason
        db.session.commit()
        db.session.expire_all()

    def cancel_json(self, recording_id):
        """A person pressing Cancel on a SCHEDULED recording: the row is deleted and
        detach_recording_references() stamps the ledger row (dev/changelog/814)."""
        resp = self.t.client.post(f'/recordings/{recording_id}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()

    def delete_from_library(self, recording_id):
        """A person deleting a finished recording from the library."""
        resp = self.t.client.post(f'/recordings/{recording_id}/delete')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, recording_id))

    # -- reading ---------------------------------------------------------------------------

    def recordings(self, rule_id):
        return (Recording.query.filter_by(rule_id=rule_id)
                .order_by(Recording.program_start_time, Recording.id).all())

    def ledger(self, rule_id):
        return (RuleMatch.query.filter_by(rule_id=rule_id)
                .order_by(RuleMatch.program_start_time, RuleMatch.id).all())

    def row_at(self, rule_id, start):
        rows = [m for m in self.ledger(rule_id) if m.program_start_time == start]
        self.assertEqual(len(rows), 1, f'expected one ledger row at {start}, found {rows}')
        return rows[0]

    def snapshot(self, rule_id):
        return [(m.id, m.action_taken, m.recording_id, m.detail, m.updated_at)
                for m in self.ledger(rule_id)]

    def open_alerts(self, alert_type, source):
        return Alert.query.filter_by(alert_type=alert_type, source=source,
                                     dismissed_at=None).all()

    def cap_alerts(self, rule_id):
        return self.open_alerts(AUTO_RULE_CAPPED, f'auto-rule:{rule_id}:cap')

    def broken_alerts(self, rule_id):
        return self.open_alerts(AUTO_RULE_BROKEN, f'auto-rule:{rule_id}:broken')


# ---------------------------------------------------------------------------
# §2 - the action table
# ---------------------------------------------------------------------------

class ActionTableTests(_LedgerCase):

    def test_six_verdicts_and_the_four_skips_are_the_ones_re_judged(self):
        """§2 names six values; `scheduled` and `user_modified` are not re-judged and the
        four skips are."""
        self.assertEqual(set(RULE_MATCH_ACTIONS), {
            'scheduled', 'skipped_existing', 'skipped_repeat', 'skipped_cap', 'skipped_limit',
            'user_modified'})
        self.assertEqual(len(RULE_MATCH_ACTIONS), 6)
        self.assertEqual(set(RULE_MATCH_SKIPS), {
            RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_SKIPPED_CAP,
            RULE_MATCH_SKIPPED_LIMIT})

    def test_a_user_modified_row_ends_the_showing_at_its_anchor(self):
        """`user_modified` is never re-judged (§2): the showing at its anchor is neither
        scheduled nor rewritten - even though the edited recording still carries the
        program's snapshot, which a re-judge would read as an existing recording."""
        ch = self.channel('Speed')
        start = _future(hours=4)
        self.showing(ch, 'Cup Race', start, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        edited = self.library('Cup Race', start, status=REC_STATUS_SCHEDULED,
                              sub_title='Sonoma', channel=ch, rule_id=rid,
                              start=start + timedelta(hours=2),
                              stop=start + timedelta(hours=3))
        self.ledger_row(rid, RULE_MATCH_USER_MODIFIED, 'Cup Race', start, sub_title='Sonoma',
                        recording=edited, channel=ch)
        before = self.snapshot(rid)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual([r.id for r in self.recordings(rid)], [edited.id])
        self.assertEqual(self.snapshot(rid), before)

    def test_evaluation_never_deletes_a_ledger_row(self):
        """A showing that leaves the guide keeps its row: only an edit (§8) and retention
        delete ledger rows (§2)."""
        ch = self.channel('Speed')
        first, second = _future(hours=4), _future(hours=8)
        entries = [self.showing(ch, 'Cup Race', first, sub_title='Sonoma'),
                   self.showing(ch, 'Cup Race', second, sub_title='Watkins Glen')]
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=1)
        self.evaluate(rid)
        before = self.snapshot(rid)
        self.assertEqual([a for _i, a, *_rest in before],
                         [RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP])

        for e in entries:
            db.session.delete(e)
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.snapshot(rid), before)


# ---------------------------------------------------------------------------
# §3.5, §11 - the NASCAR-shaped scenario
# ---------------------------------------------------------------------------

class NascarScenarioTests(_LedgerCase):
    """A Cup season seeded the way the providers list it: each race on both members of one
    group and on an unrelated channel under a "Live:" title, last week's race already
    recorded, its Wednesday replay, Saturday qualifying, and (optionally) a doubleheader whose
    second race starts inside the first race's post-padding on the same feeds."""

    SEARCH = 'grain=airings&q=NASCAR+Cup+Series+-Qualifying'

    def seed_season(self, doubleheader=True):
        now = datetime.utcnow()
        self.races = _season(now)
        self.profile = RecordingProfile(name='Race padding', pre_padding_minutes=5,
                                        post_padding_minutes=60)
        db.session.add(self.profile)
        self.east = self.channel('Cup Feed East')
        self.west = self.channel('Cup Feed West')
        self.group = seed.make_group(name='Cup Feeds', members=[self.east, self.west])
        self.simulcast = self.channel('Speed Simulcast', health_score=95)
        self.replays = self.channel('Speed Replays')
        for start, episode in zip(self.races, CUP_EPISODES):
            self.showing(self.east, 'NASCAR Cup Series ᴸᶦᵛᵉ', start, minutes=180,
                         sub_title=episode)
            self.showing(self.west, 'NASCAR Cup Series', start, minutes=180,
                         sub_title=episode)
            self.showing(self.simulcast, 'Live: NASCAR Cup Series', start, minutes=180,
                         sub_title=episode)

        # Last week's race is in the library; the guide only reaches its Wednesday replay.
        hour = now.replace(minute=0, second=0, microsecond=0)
        self.last_week = hour - timedelta(days=2)
        self.last_rec = seed.make_recording(
            status=REC_STATUS_COMPLETED, name='NASCAR Cup Series - Race at Michigan',
            channel_id=self.east.id, group_id=self.group.id, start_time=self.last_week,
            stop_time=self.last_week + timedelta(hours=3),
            program_title='NASCAR Cup Series ᴸᶦᵛᵉ', program_sub_title='Race at Michigan',
            program_start_time=self.last_week,
            program_stop_time=self.last_week + timedelta(hours=3))
        self.replay = self.last_week + timedelta(days=3)
        self.showing(self.replays, 'NASCAR Cup Series', self.replay, minutes=180,
                     sub_title='Race at Michigan')
        # And the first race's own Wednesday replay, 72 hours on: inside every limit window
        # below, so it is a repeat only if repeats are judged before the limit (§3.5).
        self.first_replay = self.races[0] + timedelta(days=3)
        self.showing(self.replays, 'NASCAR Cup Series', self.first_replay, minutes=180,
                     sub_title=CUP_EPISODES[0])

        # Saturday qualifying for the first race, under the race's own episode title.
        self.qualifying = self.races[0] - timedelta(hours=26)
        for ch in (self.east, self.west):
            self.showing(ch, 'NASCAR Cup Series Qualifying', self.qualifying, minutes=90,
                         sub_title=CUP_EPISODES[0])

        self.second_race = None
        if doubleheader:
            # Starts 3h30m after the third race, inside its 60 minutes of post-padding.
            self.second_race = self.races[2] + timedelta(hours=3, minutes=30)
            for ch in (self.east, self.west):
                self.showing(ch, 'NASCAR Cup Series', self.second_race, minutes=150,
                             sub_title='Race at Darlington II')
        db.session.commit()

    def cup_rule(self, **kw):
        kw.setdefault('max_upcoming', 0)
        return self.rule(self.SEARCH, name='Cup Series', profile_id=self.profile.id, **kw)

    def test_skip_repeats_and_no_limit_schedule_exactly_the_races_once_each_on_the_group(self):
        """§11: exactly the races are scheduled - the doubleheader too, though it starts
        inside the previous race's padded window (R1) - once each, on the group, with the
        rule's profile; the replay is a repeat and its row says of what; qualifying is left
        out by the search; and every showing the search returns has a row saying why."""
        self.seed_season()
        rid = self.cup_rule()
        out = self.evaluate(rid)

        expected = sorted(self.races + [self.second_race])
        recs = self.recordings(rid)
        self.assertEqual([r.program_start_time for r in recs], expected)
        self.assertEqual(out['scheduled'], 5)
        for rec in recs:
            with self.subTest(program_start=rec.program_start_time):
                self.assertEqual(rec.group_id, self.group.id)
                self.assertIn(rec.channel_id, {self.east.id, self.west.id})
                self.assertEqual(rec.profile_id, self.profile.id)
                self.assertEqual(rec.start_time, rec.program_start_time - timedelta(minutes=5))
                self.assertEqual(rec.stop_time, rec.program_stop_time + timedelta(minutes=60))
        third = next(r for r in recs if r.program_start_time == self.races[2])
        second = next(r for r in recs if r.program_start_time == self.second_race)
        self.assertLess(second.start_time, third.stop_time,
                        'the scenario must really overlap through padding')

        rows = self.ledger(rid)
        scheduled = [m for m in rows if m.action_taken == RULE_MATCH_SCHEDULED]
        self.assertEqual(sorted(m.program_start_time for m in scheduled), expected)
        self.assertEqual({m.group_id for m in scheduled}, {self.group.id})
        self.assertEqual(sorted(m.recording_id for m in scheduled), sorted(r.id for r in recs))
        for start in self.races:
            with self.subTest(race=start):
                self.assertIn('Speed Simulcast', self.row_at(rid, start).detail,
                              'a race row names the channel it was chosen over')

        repeat = self.row_at(rid, self.replay)
        self.assertEqual(repeat.action_taken, RULE_MATCH_SKIPPED_REPEAT)
        self.assertIn('repeat', repeat.detail.lower())
        self.assertIn(self.last_rec.name, repeat.detail)
        own_repeat = self.row_at(rid, self.first_replay)
        self.assertEqual(own_repeat.action_taken, RULE_MATCH_SKIPPED_REPEAT)
        self.assertIn('repeat', own_repeat.detail.lower())

        self.assertEqual([m for m in rows if m.program_start_time == self.qualifying], [])
        self.assertEqual(len(rows), 7, [(m.program_start_time, m.action_taken) for m in rows])

        state = auto_rules.parse_search(db.session.get(AutoRule, rid).search_params)
        result = search(replace(state, page_size=MAX_PAGE_SIZE), SearchContext.build(),
                        want_counts=False, want_facets=False)
        self.assertTrue(result.rows)
        for e in result.rows:
            with self.subTest(listing=(e.channel_id, e.start_time)):
                anchors = [m for m in rows if m.program_start_time == e.start_time
                           and m.title_key == title_key(e.title)]
                self.assertEqual(len(anchors), 1)
                self.assertTrue((anchors[0].detail or '').strip())

    def test_a_second_evaluation_of_the_season_changes_nothing(self):
        self.seed_season()
        rid = self.cup_rule()
        self.evaluate(rid)
        before = self.snapshot(rid)
        recs = [r.id for r in self.recordings(rid)]
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual([r.id for r in self.recordings(rid)], recs)
        self.assertEqual(self.snapshot(rid), before)

    def assert_limit_keeps(self, window_hours, kept):
        self.seed_season(doubleheader=False)
        rid = self.cup_rule(limit_count=1, limit_window_hours=window_hours)
        self.evaluate(rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)],
                         [self.races[i] for i in kept])
        for i, start in enumerate(self.races):
            with self.subTest(race=i + 1):
                row = self.row_at(rid, start)
                if i in kept:
                    self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
                else:
                    self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_LIMIT)
                    self.assertIn(str(window_hours), row.detail)
        # Repeats are judged before the limit (§4.3 step 4), so a replay is named as one even
        # inside the window of the race it repeats.
        for replay in (self.replay, self.first_replay):
            self.assertEqual(self.row_at(rid, replay).action_taken, RULE_MATCH_SKIPPED_REPEAT)

    def test_one_within_168_hours_loses_the_08_29_and_09_13_races(self):
        """§3.5's documented loss: the 148- and 166-hour gaps fall inside a 7-day window."""
        self.assert_limit_keeps(168, kept=(0, 2))

    def test_one_within_120_hours_keeps_all_four(self):
        self.assert_limit_keeps(120, kept=(0, 1, 2, 3))

    def test_a_pick_exactly_the_window_after_another_is_outside_it(self):
        """The window is half-open: the 08-29 race starts exactly 148 hours after the 08-23
        one, so "1 within 148 hours" keeps it."""
        self.assertEqual(self.races_gap_hours(), [148, 190, 166])
        self.assert_limit_keeps(148, kept=(0, 1, 2, 3))

    def test_one_hour_more_than_the_gap_puts_it_inside(self):
        self.assert_limit_keeps(149, kept=(0, 2, 3))

    def races_gap_hours(self):
        races = _season(datetime.utcnow())
        return [int((b - a).total_seconds() // 3600) for a, b in zip(races, races[1:])]


# ---------------------------------------------------------------------------
# §3.4 - skip repeats
# ---------------------------------------------------------------------------

class RepeatTests(_LedgerCase):

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.race = _future(hours=4)
        self.replay = _future(hours=30)

    def test_an_earlier_airing_in_the_library_makes_a_repeat_by_its_status(self):
        """A recording of an earlier airing, any provenance (here made by hand), counts when
        it is SCHEDULED, live or done; FAILED and ABORTED do not count (§3.4)."""
        now = datetime.utcnow().replace(second=0, microsecond=0)
        cases = ([(s, True) for s in (REC_STATUS_SCHEDULED,) + tuple(REC_LIVE) + tuple(REC_DONE)]
                 + [(s, False) for s in REC_DEAD])
        for word, (status, counts) in zip(_WORDS, cases):
            with self.subTest(status=status):
                title = f'{word} Cup Race'
                earlier = (now + timedelta(hours=2) if status == REC_STATUS_SCHEDULED
                           else now - timedelta(days=2))
                original = self.library(
                    title, earlier, status=status, sub_title='Sonoma',
                    channel=self.channel(f'{word} Feed'),
                    cancel_reason=(CANCEL_DURING_CAPTURE if status == REC_STATUS_ABORTED
                                   else None))
                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                row = self.row_at(rid, self.replay)
                if counts:
                    self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_REPEAT)
                    self.assertIn(original.name, row.detail)
                    self.assertEqual(self.recordings(rid), [])
                else:
                    self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
                    self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_showing_without_an_episode_title_is_never_a_repeat(self):
        """A bare series title would match every week's race; skipping a real race is far
        worse than recording an extra replay (§3.4)."""
        past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
        self.library('Cup Race', past)
        self.library('Cup Race', past - timedelta(days=7), sub_title='Sonoma')
        self.showing(self.ch, 'Cup Race', self.replay)
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, self.replay).action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_later_recording_never_makes_an_earlier_airing_a_repeat(self):
        """A recording of a LATER showing, made by hand, never turns the earlier showing
        into a repeat (§3.4)."""
        later = self.library('Cup Race', self.replay, status=REC_STATUS_SCHEDULED,
                             sub_title='Sonoma', channel=self.channel('Speed Replays'))
        self.showing(self.ch, 'Cup Race', self.race, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, self.race).action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(db.session.get(Recording, later.id).status, REC_STATUS_SCHEDULED)

    def test_the_rules_own_later_pick_never_makes_an_earlier_airing_a_repeat(self):
        """Sources refresh to different reaches: the rule may pick the later airing before
        the earlier one reaches the guide. Both are recorded and nothing cancels the later
        one (§3.4)."""
        self.showing(self.channel('Speed Replays'), 'Cup Race', self.replay, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        later = self.recordings(rid)
        self.assertEqual([r.program_start_time for r in later], [self.replay])

        self.showing(self.ch, 'Cup Race', self.race, sub_title='Sonoma')
        db.session.commit()
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual([r.program_start_time for r in recs], [self.race, self.replay])
        self.assertEqual({r.status for r in recs}, {REC_STATUS_SCHEDULED})
        self.assertEqual(self.row_at(rid, self.replay).recording_id, later[0].id)

    def test_a_failed_original_is_re_judged_and_its_replay_scheduled(self):
        """R8: the replay's `skipped_repeat` row is re-judged once the race it repeats
        fails, and the replay is scheduled on that same row; the failed race's own row is
        not re-judged, so the race is not recorded again."""
        self.showing(self.ch, 'Cup Race', self.race, sub_title='Sonoma')
        self.showing(self.channel('Speed Replays'), 'Cup Race', self.replay, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        race_rec = self.recordings(rid)
        self.assertEqual([r.program_start_time for r in race_rec], [self.race])
        replay_row = self.row_at(rid, self.replay)
        self.assertEqual(replay_row.action_taken, RULE_MATCH_SKIPPED_REPEAT)
        replay_row_id = replay_row.id

        self.set_status(race_rec[0].id, REC_STATUS_FAILED)
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual([(r.program_start_time, r.status) for r in recs],
                         [(self.race, REC_STATUS_FAILED), (self.replay, REC_STATUS_SCHEDULED)])
        row = self.row_at(rid, self.replay)
        self.assertEqual((row.id, row.action_taken, row.recording_id),
                         (replay_row_id, RULE_MATCH_SCHEDULED, recs[1].id))
        self.assertEqual(self.row_at(rid, self.race).recording_id, race_rec[0].id)

    def test_the_rules_own_pick_cancelled_by_a_person_still_makes_repeats(self):
        """R6: a pick a person aborted - for any of the four person reasons - is still the
        program dealt with, so a later airing of it is a repeat and the pick is not made
        again (§3.4)."""
        for word, reason in zip(_WORDS, _BY_A_PERSON):
            with self.subTest(cancel_reason=reason):
                title = f'{word} Cup Race'
                q = f'grain=airings&q={word}+Cup+Race'
                self.showing(self.channel(f'{word} Feed'), title, self.race, sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(q, max_upcoming=0, name=word)
                self.evaluate(rid)
                (pick,) = self.recordings(rid)
                self.set_status(pick.id, REC_STATUS_ABORTED, reason)

                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                self.evaluate(rid)
                self.assertEqual(self.row_at(rid, self.replay).action_taken,
                                 RULE_MATCH_SKIPPED_REPEAT)
                self.assertEqual([r.id for r in self.recordings(rid)], [pick.id])
                self.assertEqual(self.row_at(rid, self.race).recording_id, pick.id)

    def test_the_rules_own_pick_deleted_while_scheduled_still_makes_repeats(self):
        """A pick cancelled while SCHEDULED is deleted; its row keeps the fate
        detach_recording_references() stamped, and that fate still counts (§2, §3.4)."""
        self.showing(self.ch, 'Cup Race', self.race, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        (pick,) = self.recordings(rid)
        self.cancel_json(pick.id)
        row = self.row_at(rid, self.race)
        self.assertEqual((row.action_taken, row.recording_id, row.removed_status,
                          row.removed_cancel_reason),
                         (RULE_MATCH_SCHEDULED, None, REC_STATUS_SCHEDULED, None))

        self.showing(self.channel('Speed Replays'), 'Cup Race', self.replay, sub_title='Sonoma')
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, self.replay).action_taken, RULE_MATCH_SKIPPED_REPEAT)
        self.assertEqual(self.recordings(rid), [])

    def test_the_rules_own_pick_watched_or_aborted_and_then_deleted_still_makes_repeats(self):
        """Once the pick is deleted from the library its fate is read from what
        detach_recording_references() stamped: recorded and deleted, or aborted by a person
        and deleted, still counts (§2, §3.4)."""
        for word, (status, reason) in zip(_WORDS, ((REC_STATUS_COMPLETED, None),
                                                   (REC_STATUS_ABORTED, CANCEL_DURING_CAPTURE))):
            with self.subTest(status=status, cancel_reason=reason):
                title = f'{word} Cup Race'
                self.showing(self.channel(f'{word} Feed'), title, self.race, sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                (pick,) = self.recordings(rid)
                self.set_status(pick.id, status, reason)
                self.delete_from_library(pick.id)
                row = self.row_at(rid, self.race)
                self.assertEqual((row.recording_id, row.removed_status,
                                  row.removed_cancel_reason), (None, status, reason))

                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                self.evaluate(rid)
                self.assertEqual(self.row_at(rid, self.replay).action_taken,
                                 RULE_MATCH_SKIPPED_REPEAT)
                self.assertEqual(self.recordings(rid), [])

    def test_the_library_only_remembers_what_is_still_in_it(self):
        """A showing whose only earlier recording was made by hand and then deleted is not a
        repeat; nor is one whose earlier recording was another rule's pick, cancelled - a
        rule remembers only its own picks (§3.4)."""
        past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
        manual = self.library('Alpha Cup Race', past, sub_title='Sonoma')
        self.delete_from_library(manual.id)
        self.showing(self.channel('Alpha Replays'), 'Alpha Cup Race', self.replay,
                     sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Alpha+Cup+Race', max_upcoming=0, name='alpha')
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, self.replay).action_taken, RULE_MATCH_SCHEDULED)

        self.showing(self.channel('Bravo Feed'), 'Bravo Cup Race', self.race, sub_title='Sonoma')
        replays = self.channel('Bravo Replays')
        self.showing(replays, 'Bravo Cup Race', self.replay, sub_title='Sonoma')
        db.session.commit()
        races = self.rule('grain=airings&q=Bravo+Cup+Race&x.chan=' + str(replays.id),
                          max_upcoming=0, name='races')
        self.evaluate(races)
        (pick,) = self.recordings(races)
        self.cancel_json(pick.id)
        replay_rule = self.rule(f'grain=airings&q=Bravo+Cup+Race&f.chan={replays.id}',
                                max_upcoming=0, name='replays')
        self.evaluate(replay_rule)
        self.assertEqual(self.row_at(replay_rule, self.replay).action_taken,
                         RULE_MATCH_SCHEDULED)

    def test_a_pick_aborted_by_a_group_change_is_not_a_repeat(self):
        """A group edit aborting the pick is not the user deciding against the program
        (§3.4): a later airing is scheduled. The same pick aborted by a person is a repeat."""
        past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
        for word, reason, expected in ((_WORDS[0], CANCEL_GROUP_CHANGE, RULE_MATCH_SCHEDULED),
                                       (_WORDS[1], CANCEL_BEFORE_START,
                                        RULE_MATCH_SKIPPED_REPEAT)):
            with self.subTest(cancel_reason=reason):
                title = f'{word} Cup Race'
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                feed = self.channel(f'{word} Feed')
                pick = self.library(title, past, status=REC_STATUS_ABORTED, sub_title='Sonoma',
                                    channel=feed, cancel_reason=reason, rule_id=rid)
                self.ledger_row(rid, RULE_MATCH_SCHEDULED, title, past, sub_title='Sonoma',
                                recording=pick, channel=feed)
                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                self.evaluate(rid)
                self.assertEqual(self.row_at(rid, self.replay).action_taken, expected)

    def test_a_pick_aborted_by_a_group_change_is_re_judged_and_rescheduled(self):
        """§2's one exception: a `scheduled` row whose recording a group change aborted is
        judged again - also once that recording is deleted, from its stamped fate - and the
        program is scheduled again on the same row."""
        for word, deleted in ((_WORDS[0], False), (_WORDS[1], True)):
            with self.subTest(deleted=deleted):
                title = f'{word} Cup Race'
                feed = self.channel(f'{word} Feed East')
                group = seed.make_group(name=f'{word} Feeds', members=[feed])
                self.showing(feed, title, self.race, sub_title='Sonoma')
                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                (first,) = self.recordings(rid)
                self.assertEqual(first.group_id, group.id)
                row_id = self.row_at(rid, self.race).id

                self.set_status(first.id, REC_STATUS_ABORTED, CANCEL_GROUP_CHANGE)
                if deleted:
                    self.delete_from_library(first.id)
                out = self.evaluate(rid)
                self.assertEqual(out['scheduled'], 1)
                recs = self.recordings(rid)
                self.assertEqual([(r.program_start_time, r.status) for r in recs],
                                 ([] if deleted else [(self.race, REC_STATUS_ABORTED)])
                                 + [(self.race, REC_STATUS_SCHEDULED)])
                row = self.row_at(rid, self.race)
                self.assertEqual((row.id, row.action_taken, row.recording_id),
                                 (row_id, RULE_MATCH_SCHEDULED, recs[-1].id))
                self.assertEqual(recs[-1].group_id, group.id)
                self.assertEqual(self.row_at(rid, self.replay).action_taken,
                                 RULE_MATCH_SKIPPED_REPEAT)

    def test_a_recording_named_by_a_user_modified_row_is_not_an_earlier_airing(self):
        """The program moved away from that recording's slot (§5), so it is not an earlier
        airing of anything - whichever rule's `user_modified` row names it (§3.4)."""
        for word, own in ((_WORDS[0], True), (_WORDS[1], False)):
            with self.subTest(own_rule=own):
                title = f'{word} Cup Race'
                earlier = _future(hours=2)
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                owner = rid if own else self.rule(f'grain=airings&q={word}+Other',
                                                  max_upcoming=0, name=f'{word} other')
                feed = self.channel(f'{word} Feed')
                edited = self.library(title, earlier, status=REC_STATUS_SCHEDULED,
                                      sub_title='Sonoma', channel=feed, rule_id=owner,
                                      start=earlier + timedelta(hours=1),
                                      stop=earlier + timedelta(hours=2))
                self.ledger_row(owner, RULE_MATCH_USER_MODIFIED, title, earlier,
                                sub_title='Sonoma', recording=edited, channel=feed)
                self.showing(self.channel(f'{word} Replays'), title, self.replay,
                             sub_title='Sonoma')
                db.session.commit()
                self.evaluate(rid)
                self.assertEqual(self.row_at(rid, self.replay).action_taken,
                                 RULE_MATCH_SCHEDULED)


# ---------------------------------------------------------------------------
# §4.3 step 4.2 - the existing check, R1 and R2
# ---------------------------------------------------------------------------

class ExistingCheckTests(_LedgerCase):

    def test_the_same_program_already_recorded_on_any_channel_skips_it(self):
        """Any provenance, any channel, status SCHEDULED, live or done - matched as the same
        program through the title marks; FAILED and ABORTED do not count."""
        cases = ([(s, True) for s in (REC_STATUS_SCHEDULED,) + tuple(REC_LIVE) + tuple(REC_DONE)]
                 + [(s, False) for s in REC_DEAD])
        for word, (status, counts) in zip(_WORDS, cases):
            with self.subTest(status=status):
                start = _future(hours=4)
                title = f'{word} Cup Race'
                other = self.library(
                    f'{title} ᴸᶦᵛᵉ', start, status=status, sub_title='Sonoma',
                    channel=self.channel(f'{word} Elsewhere'),
                    cancel_reason=(CANCEL_BEFORE_START if status == REC_STATUS_ABORTED
                                   else None))
                self.showing(self.channel(f'{word} Feed'), title, start, sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                row = self.row_at(rid, start)
                if counts:
                    self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
                    self.assertIn(f'#{other.id}', row.detail)
                    self.assertEqual(self.recordings(rid), [])
                else:
                    self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)

    def test_a_same_channel_recording_covering_the_whole_air_time_skips_it(self):
        """A time-based recording made by hand covers the showing whatever it was called."""
        ch = self.channel('Speed')
        start = _future(hours=4)
        manual = seed.make_recording(status=REC_STATUS_SCHEDULED, name='Speed Sunday block',
                                     channel_id=ch.id, start_time=start - timedelta(minutes=10),
                                     stop_time=start + timedelta(minutes=70))
        self.showing(ch, 'Cup Race', start, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        row = self.row_at(rid, start)
        self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertIn(f'#{manual.id}', row.detail)
        self.assertEqual(self.recordings(rid), [])

    def test_a_group_recording_covering_the_whole_air_time_skips_the_group_row(self):
        east, west = self.channel('Cup Feed East'), self.channel('Cup Feed West')
        group = seed.make_group(name='Cup Feeds', members=[east, west])
        start = _future(hours=4)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Cup Feeds block',
                            channel_id=west.id, group_id=group.id,
                            start_time=start - timedelta(minutes=10),
                            stop_time=start + timedelta(minutes=70))
        self.showing(east, 'Cup Race', start, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, start).action_taken, RULE_MATCH_SKIPPED_EXISTING)

    def test_a_recording_overlapping_only_through_padding_is_a_handoff(self):
        """R1: back-to-back recordings on one channel always overlap through padding, so a
        partial overlap is never a reason to skip; nor is a recording on another channel
        covering the same time."""
        ch = self.channel('Speed')
        start = _future(hours=4)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Earlier show, padded',
                            channel_id=ch.id, start_time=start - timedelta(hours=1),
                            stop_time=start + timedelta(minutes=15))
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Later show, padded',
                            channel_id=ch.id, start_time=start + timedelta(minutes=50),
                            stop_time=start + timedelta(hours=2))
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Another channel',
                            channel_id=self.channel('Elsewhere').id,
                            start_time=start - timedelta(hours=1),
                            stop_time=start + timedelta(hours=2))
        self.showing(ch, 'Cup Race', start, sub_title='Sonoma')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, start).action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_person_first_then_the_rule_and_cancelling_the_person_hands_it_over(self):
        """R2: the first recording made wins on any channel; the rule's showing is
        `skipped_existing`, and once the winner is cancelled - deleted, or ABORTED - the
        rule takes the program over on its same row."""
        for word, how in ((_WORDS[0], 'deleted'), (_WORDS[1], 'aborted')):
            with self.subTest(cancelled=how):
                start = _future(hours=4)
                title = f'{word} Cup Race'
                person = self.library(title, start, status=REC_STATUS_SCHEDULED,
                                      sub_title='Sonoma', channel=self.channel(f'{word} Mine'))
                self.showing(self.channel(f'{word} Feed'), title, start, sub_title='Sonoma')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                row = self.row_at(rid, start)
                self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
                row_id = row.id

                if how == 'deleted':
                    self.cancel_json(person.id)
                else:
                    self.set_status(person.id, REC_STATUS_ABORTED, CANCEL_BEFORE_START)
                self.evaluate(rid)
                row = self.row_at(rid, start)
                recs = self.recordings(rid)
                self.assertEqual(len(recs), 1)
                self.assertEqual((row.id, row.action_taken, row.recording_id),
                                 (row_id, RULE_MATCH_SCHEDULED, recs[0].id))

    def test_two_rules_on_one_program_the_first_made_wins(self):
        """R2 between rules: the second is `skipped_existing` on the first one's recording;
        when that is cancelled the second takes over, and the first never recreates it."""
        start = _future(hours=4)
        self.showing(self.channel('Speed'), 'Cup Race', start, sub_title='Sonoma')
        db.session.commit()
        first = self.rule('grain=airings&q=Cup+Race', max_upcoming=0, name='first')
        second = self.rule('grain=airings&q=Cup+Race+Sonoma', max_upcoming=0, name='second')
        self.evaluate(first)
        self.evaluate(second)
        (winner,) = self.recordings(first)
        self.assertEqual(self.recordings(second), [])
        loser_row = self.row_at(second, start)
        self.assertEqual(loser_row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertIn(f'#{winner.id}', loser_row.detail)

        self.cancel_json(winner.id)
        self.evaluate(second)
        self.evaluate(first)
        self.assertEqual(len(self.recordings(second)), 1)
        self.assertEqual(self.row_at(second, start).action_taken, RULE_MATCH_SCHEDULED)
        self.assertEqual(self.recordings(first), [])
        row = self.row_at(first, start)
        self.assertEqual((row.action_taken, row.recording_id, row.removed_status),
                         (RULE_MATCH_SCHEDULED, None, REC_STATUS_SCHEDULED))


# ---------------------------------------------------------------------------
# §2, §3.5, §3.6 - ledger and counts
# ---------------------------------------------------------------------------

class LedgerAndCountTests(_LedgerCase):

    def seed_showings(self, n, title='Cup Race', hours_apart=4):
        ch = self.channel(f'{title} Channel')
        starts = [_future(hours=4 + i * hours_apart) for i in range(n)]
        for i, start in enumerate(starts):
            self.showing(ch, title, start, sub_title=f'Round {i + 1}')
        db.session.commit()
        return starts

    def test_a_second_evaluation_creates_nothing_and_rewrites_nothing(self):
        starts = self.seed_showings(3)
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0)
        self.assertEqual(self.evaluate(rid)['scheduled'], 3)
        before = self.snapshot(rid)
        recs = [r.id for r in self.recordings(rid)]

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual([r.id for r in self.recordings(rid)], recs)
        self.assertEqual(self.snapshot(rid), before)
        self.assertEqual([m.program_start_time for m in self.ledger(rid)], starts)

    def test_a_cancelled_rule_recording_is_never_recreated(self):
        """R6: a pick deleted while SCHEDULED - by the Cancel route or the shared
        delete-row helper - leaves its `scheduled` row, stamped with the fate, and no later
        evaluation makes it again."""
        from app.routes.recordings import _unschedule_and_delete_row
        for word, how in ((_WORDS[0], 'cancel route'), (_WORDS[1], 'delete-row helper')):
            with self.subTest(how=how):
                (start,) = self.seed_showings(1, title=f'{word} Cup Race')
                rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, name=word)
                self.evaluate(rid)
                (pick,) = self.recordings(rid)
                if how == 'cancel route':
                    self.cancel_json(pick.id)
                    self.unscheduled_by_cancel.assert_any_call(pick.id)
                else:
                    _unschedule_and_delete_row(pick.id)
                    db.session.expire_all()
                self.assertIsNone(db.session.get(Recording, pick.id))

                for _ in range(2):
                    self.assertEqual(self.evaluate(rid)['scheduled'], 0)
                self.assertEqual(self.recordings(rid), [])
                row = self.row_at(rid, start)
                self.assertEqual((row.action_taken, row.recording_id, row.removed_status,
                                  row.removed_cancel_reason),
                                 (RULE_MATCH_SCHEDULED, None, REC_STATUS_SCHEDULED, None))

    def test_a_broad_first_evaluation_stops_at_the_default_cap(self):
        """§3.6: at auto_scheduling.default_max_upcoming SCHEDULED recordings - counting the
        ones this run made - the rest are `skipped_cap` and the cap alert is raised."""
        self.t.sandbox_config({'auto_scheduling': {'default_max_upcoming': 3}})
        starts = self.seed_showings(5)
        rid = self.rule('grain=airings&q=Cup+Race')
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 3)
        self.assertEqual(out['skipped'], {RULE_MATCH_SKIPPED_CAP: 2})
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], starts[:3])
        for start in starts[3:]:
            row = self.row_at(rid, start)
            self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_CAP)
            self.assertTrue(row.detail)
        self.assertEqual(len(self.cap_alerts(rid)), 1)

    def test_the_rules_own_cap_overrides_the_default(self):
        self.t.sandbox_config({'auto_scheduling': {'default_max_upcoming': 3}})
        self.seed_showings(5)
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=2)
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 2)
        self.assertEqual(out['skipped'], {RULE_MATCH_SKIPPED_CAP: 3})
        self.assertEqual(len(self.cap_alerts(rid)), 1)

    def test_a_cap_skip_is_re_judged_once_the_cap_frees_and_the_alert_clears(self):
        starts = self.seed_showings(4)
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=3)
        self.evaluate(rid)
        held = self.row_at(rid, starts[3])
        self.assertEqual(held.action_taken, RULE_MATCH_SKIPPED_CAP)
        held_id = held.id
        self.assertEqual(len(self.cap_alerts(rid)), 1)

        first = self.recordings(rid)[0]
        self.set_status(first.id, REC_STATUS_COMPLETED)
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        row = self.row_at(rid, starts[3])
        self.assertEqual((row.id, row.action_taken), (held_id, RULE_MATCH_SCHEDULED))
        self.assertEqual(self.cap_alerts(rid), [])
        self.assertEqual(Alert.query.filter(
            Alert.alert_type == AUTO_RULE_CAPPED, Alert.source == f'auto-rule:{rid}:cap',
            Alert.dismissed_at.isnot(None)).count(), 1)

    def limited_rule(self, word):
        """Two showings 24 hours apart under "1 within 48 hours": the first is picked and the
        second held by the limit."""
        starts = self.seed_showings(2, title=f'{word} Cup Race', hours_apart=24)
        rid = self.rule(f'grain=airings&q={word}+Cup+Race', max_upcoming=0, limit_count=1,
                        limit_window_hours=48, name=word)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, starts[1]).action_taken, RULE_MATCH_SKIPPED_LIMIT)
        (pick,) = self.recordings(rid)
        return rid, starts, pick

    def test_a_pick_cancelled_before_it_started_frees_its_limit_slot(self):
        """§3.5: a pick that never started - deleted while SCHEDULED, or ABORTED before its
        start - stops counting, so the held showing is scheduled at the next evaluation; the
        cancelled pick itself is not made again."""
        for word, how in ((_WORDS[0], 'deleted'), (_WORDS[1], 'aborted before start')):
            with self.subTest(cancelled=how):
                rid, starts, pick = self.limited_rule(word)
                held_id = self.row_at(rid, starts[1]).id
                if how == 'deleted':
                    self.cancel_json(pick.id)
                else:
                    self.set_status(pick.id, REC_STATUS_ABORTED, CANCEL_BEFORE_START)
                out = self.evaluate(rid)
                self.assertEqual(out['scheduled'], 1)
                row = self.row_at(rid, starts[1])
                self.assertEqual((row.id, row.action_taken), (held_id, RULE_MATCH_SCHEDULED))
                self.assertEqual([r.program_start_time for r in self.recordings(rid)
                                  if r.status == REC_STATUS_SCHEDULED], [starts[1]])

    def test_a_failed_or_started_pick_keeps_its_limit_slot(self):
        """§3.5: a suppressed showing is never promoted because the pick that took its slot
        failed - the limit must not become a retry. A pick aborted after it started keeps
        counting too, and so does one recorded and then deleted from the library."""
        cases = ((REC_STATUS_FAILED, None, False),
                 (REC_STATUS_ABORTED, CANCEL_DURING_CAPTURE, False),
                 (REC_STATUS_COMPLETED, None, True))
        for word, (status, reason, deleted) in zip(_WORDS, cases):
            with self.subTest(status=status, cancel_reason=reason, deleted=deleted):
                rid, starts, pick = self.limited_rule(word)
                self.set_status(pick.id, status, reason)
                if deleted:
                    self.delete_from_library(pick.id)
                before = self.snapshot(rid)
                out = self.evaluate(rid)
                self.assertEqual(out['scheduled'], 0)
                self.assertEqual(self.row_at(rid, starts[1]).action_taken,
                                 RULE_MATCH_SKIPPED_LIMIT)
                self.assertEqual(self.snapshot(rid), before)

    def test_a_user_modified_row_is_not_a_counted_pick(self):
        """§3.5: the pick made at the program's new time is what counts, never the row the
        user's edit left behind."""
        ch = self.channel('Speed')
        moved_from = _future(hours=4)
        start = _future(hours=20)
        self.showing(ch, 'Cup Race', start, sub_title='Round 2')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0, limit_count=1,
                        limit_window_hours=48)
        edited = self.library('Cup Race', moved_from, status=REC_STATUS_SCHEDULED,
                              sub_title='Round 1', channel=ch, rule_id=rid,
                              start=moved_from + timedelta(hours=1),
                              stop=moved_from + timedelta(hours=2))
        self.ledger_row(rid, RULE_MATCH_USER_MODIFIED, 'Cup Race', moved_from,
                        sub_title='Round 1', recording=edited, channel=ch)
        self.evaluate(rid)
        self.assertEqual(self.row_at(rid, start).action_taken, RULE_MATCH_SCHEDULED)

    def test_an_unchanged_verdict_is_not_rewritten(self):
        """A re-judged row that reaches the same verdict is not written again (§2):
        `updated_at` and the detail stay exactly as they were."""
        past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
        ch = self.channel('Speed')
        self.library('Cup Race', past, sub_title='Round 1')
        starts = [_future(hours=4 + 4 * i) for i in range(4)]
        self.showing(ch, 'Cup Race', starts[0], sub_title='Round 1')   # a repeat
        self.showing(ch, 'Cup Race', starts[1], sub_title='Round 2')   # picked
        self.showing(ch, 'Cup Race', starts[2], sub_title='Round 3')   # over the limit
        self.showing(ch, 'Cup Race', starts[3], sub_title='Round 4')   # over the limit
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='Elsewhere', program_title='Cup Race',
                            program_sub_title='Round 5', channel_id=self.channel('Other').id,
                            program_start_time=starts[3] + timedelta(hours=4),
                            program_stop_time=starts[3] + timedelta(hours=5),
                            start_time=starts[3] + timedelta(hours=4),
                            stop_time=starts[3] + timedelta(hours=5))
        self.showing(ch, 'Cup Race', starts[3] + timedelta(hours=4), sub_title='Round 5')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0, limit_count=1,
                        limit_window_hours=96)
        self.evaluate(rid)
        actions = [m.action_taken for m in self.ledger(rid)]
        self.assertEqual(actions, [RULE_MATCH_SKIPPED_REPEAT, RULE_MATCH_SCHEDULED,
                                   RULE_MATCH_SKIPPED_LIMIT, RULE_MATCH_SKIPPED_LIMIT,
                                   RULE_MATCH_SKIPPED_EXISTING])
        before = self.snapshot(rid)
        for _ in range(2):
            self.evaluate(rid)
            self.assertEqual(self.snapshot(rid), before)


# ---------------------------------------------------------------------------
# §8 - edit
# ---------------------------------------------------------------------------

class EditTests(_LedgerCase):

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.starts = [_future(hours=4 + 4 * i) for i in range(3)]
        for start, episode in zip(self.starts, ('Alpha', 'Bravo', 'Charlie')):
            self.showing(self.ch, 'Cup Race', start, sub_title=episode)
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=1)
        self.evaluate(self.rid)
        self.past = datetime.utcnow().replace(second=0, microsecond=0) - timedelta(days=2)
        self.past_row = self.ledger_row(self.rid, RULE_MATCH_SKIPPED_REPEAT, 'Cup Race',
                                        self.past, sub_title='Zulu', channel=self.ch).id
        moved = _future(hours=40)
        edited = self.library('Cup Race', moved, status=REC_STATUS_SCHEDULED,
                              sub_title='Yankee', channel=self.ch, rule_id=self.rid,
                              start=moved + timedelta(hours=1), stop=moved + timedelta(hours=2))
        self.user_row = self.ledger_row(self.rid, RULE_MATCH_USER_MODIFIED, 'Cup Race', moved,
                                        sub_title='Yankee', recording=edited, channel=self.ch).id
        (self.kept,) = [r for r in self.recordings(self.rid)
                        if r.program_start_time == self.starts[0]]
        self.assertEqual([self.row_at(self.rid, s).action_taken for s in self.starts],
                         [RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP, RULE_MATCH_SKIPPED_CAP])
        self.assertEqual(len(self.cap_alerts(self.rid)), 1)

    def future_skip_ids(self):
        return [self.row_at(self.rid, s).id for s in self.starts[1:]]

    def test_an_edit_keeps_what_it_scheduled_and_judges_its_skips_afresh(self):
        """§8 through the edit route: the revision moves, the rule's skip rows for future
        showings go, `scheduled` and `user_modified` rows and a past skip stay, and the
        recording it already made is kept though the edited search no longer matches it."""
        skip_ids = self.future_skip_ids()
        resp = self.t.client.put(f'/api/auto-rules/{self.rid}', json={
            'search': 'grain=airings&q=Cup+Race+-Alpha', 'max_upcoming': 0})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()

        rule = db.session.get(AutoRule, self.rid)
        self.assertEqual(rule.revision, 2)
        kept = db.session.get(Recording, self.kept.id)
        self.assertEqual((kept.status, kept.rule_id), (REC_STATUS_SCHEDULED, self.rid))
        self.assertEqual(self.row_at(self.rid, self.starts[0]).action_taken,
                         RULE_MATCH_SCHEDULED)
        self.assertEqual(db.session.get(RuleMatch, self.past_row).action_taken,
                         RULE_MATCH_SKIPPED_REPEAT)
        self.assertEqual(db.session.get(RuleMatch, self.user_row).action_taken,
                         RULE_MATCH_USER_MODIFIED)
        for row_id in skip_ids:
            self.assertIsNone(db.session.get(RuleMatch, row_id),
                              'a future skip row is deleted, not re-judged in place')
        self.assertEqual([self.row_at(self.rid, s).action_taken for s in self.starts[1:]],
                         [RULE_MATCH_SCHEDULED, RULE_MATCH_SCHEDULED])
        self.assertEqual(resp.get_json()['evaluation']['scheduled'], 2)
        self.assertEqual(self.cap_alerts(self.rid), [])

    def test_the_revision_and_the_skip_deletion_land_in_one_commit(self):
        """No committed state of the database ever shows the edit half done: every commit
        update_rule() makes shows either the old revision with its skip rows or the new
        revision without them (§8)."""
        skip_ids = self.future_skip_ids()
        rule = db.session.get(AutoRule, self.rid)
        values = auto_rules.read_rule_values(
            {'search': 'grain=airings&q=Cup+Race+-Alpha', 'max_upcoming': 0}, existing=rule)
        marks = ','.join('?' * len(skip_ids))
        seen = []

        def _after_commit(_session):
            con = sqlite3.connect(self.t.db_path)
            try:
                revision = con.execute('SELECT revision FROM auto_rules WHERE id = ?',
                                       (self.rid,)).fetchone()[0]
                skips = con.execute(f'SELECT COUNT(*) FROM rule_matches WHERE id IN ({marks})',
                                    skip_ids).fetchone()[0]
            finally:
                con.close()
            seen.append((revision, skips))

        event.listen(Session, 'after_commit', _after_commit)
        try:
            self.assertTrue(auto_rules.update_rule(self.rid, values))
        finally:
            event.remove(Session, 'after_commit', _after_commit)
        self.assertIn((2, 0), seen)
        self.assertEqual([s for s in seen if s not in ((1, len(skip_ids)), (2, 0))], [],
                         f'a commit showed the edit half done: {seen}')
        db.session.expire_all()
        self.assertEqual(self.cap_alerts(self.rid), [],
                         'an edit clears the rule\'s alerts by itself, before any evaluation')

    def test_an_edit_of_a_paused_rule_does_not_evaluate_it(self):
        self.t.client.post(f'/api/auto-rules/{self.rid}/pause')
        resp = self.t.client.put(f'/api/auto-rules/{self.rid}', json={'max_upcoming': 0})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIsNone(resp.get_json()['evaluation'])
        db.session.expire_all()
        self.assertEqual(db.session.get(AutoRule, self.rid).revision, 3)
        self.assertEqual(len(self.recordings(self.rid)), 2)


# ---------------------------------------------------------------------------
# §8 - delete
# ---------------------------------------------------------------------------

class DeleteTests(_LedgerCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.starts = [_future(hours=4 + 4 * i) for i in range(4)]
        for i, start in enumerate(self.starts):
            self.showing(ch, 'Cup Race', start, sub_title=f'Round {i + 1}')
        other = self.channel('Weather')
        self.showing(other, 'Storm Watch', _future(hours=5))
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=3)
        self.other_rid = self.rule('grain=airings&q=Storm+Watch', max_upcoming=0, name='other')
        self.evaluate(self.rid)
        self.evaluate(self.other_rid)
        create_alert(AUTO_RULE_BROKEN, title='broken', body='seeded',
                     source=f'auto-rule:{self.rid}:broken')
        recs = self.recordings(self.rid)
        self.assertEqual(len(recs), 3)
        self.live, self.done, self.waiting = recs
        self.set_status(self.live.id, REC_STATUS_IN_PROGRESS)
        self.set_status(self.done.id, REC_STATUS_COMPLETED)
        self.other_rows = self.snapshot(self.other_rid)
        self.assertEqual(len(self.cap_alerts(self.rid)), 1)
        self.assertEqual(len(self.broken_alerts(self.rid)), 1)

    def delete(self, body):
        resp = self.t.client.delete(f'/api/auto-rules/{self.rid}', json=body)
        db.session.expire_all()
        return resp

    def assert_rule_gone(self):
        self.assertIsNone(db.session.get(AutoRule, self.rid))
        self.assertEqual(self.ledger(self.rid), [])
        self.assertEqual(self.cap_alerts(self.rid), [])
        self.assertEqual(self.broken_alerts(self.rid), [])
        self.assertEqual(self.snapshot(self.other_rid), self.other_rows)
        for rec_id, status in ((self.live.id, REC_STATUS_IN_PROGRESS),
                               (self.done.id, REC_STATUS_COMPLETED)):
            rec = db.session.get(Recording, rec_id)
            self.assertEqual((rec.status, rec.rule_id), (status, None))
            event_row = RecordingEvent.query.filter_by(
                recording_id=rec_id, event_type=RECORDING_RULE_SCHEDULED).one()
            self.assertIn('Test rule', event_row.detail,
                          'the survivor\'s own event still names the rule')

    def committed_state(self):
        """(rule row present, its ledger rows, its recordings, the SCHEDULED one present) as
        committed - read on a connection of its own, never through the session."""
        con = sqlite3.connect(self.t.db_path)
        try:
            one = con.execute
            return (one('SELECT COUNT(*) FROM auto_rules WHERE id = ?', (self.rid,)).fetchone()[0],
                    one('SELECT COUNT(*) FROM rule_matches WHERE rule_id = ?',
                        (self.rid,)).fetchone()[0],
                    one('SELECT COUNT(*) FROM recordings WHERE rule_id = ?',
                        (self.rid,)).fetchone()[0],
                    one('SELECT COUNT(*) FROM recordings WHERE id = ?',
                        (self.waiting.id,)).fetchone()[0])
        finally:
            con.close()

    def test_cancel_scheduled_true_cancels_only_what_has_not_started(self):
        """The SCHEDULED recording is cancelled - deleted, then unscheduled once the delete
        has committed; the started and finished ones survive without their rule_id."""
        at_unschedule = []
        self.unscheduled.side_effect = lambda rid: at_unschedule.append(self.committed_state())
        resp = self.delete({'cancel_scheduled': True})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {'success': True, 'cancelled': 1, 'kept': 2})
        self.assertIsNone(db.session.get(Recording, self.waiting.id))
        self.unscheduled.assert_called_once_with(self.waiting.id)
        self.assertEqual(at_unschedule, [(0, 0, 0, 0)],
                         'unscheduled only after the delete committed')
        self.assert_rule_gone()

    def test_the_delete_lands_all_at_once(self):
        """One unit (§8): no commit ever shows the rule half deleted - its row, its ledger,
        its recordings' rule_id and the cancelled recording go together."""
        before = self.committed_state()
        self.assertEqual(before, (1, 4, 3, 1))
        seen = []

        def _after_commit(_session):
            seen.append(self.committed_state())

        event.listen(Session, 'after_commit', _after_commit)
        try:
            self.delete({'cancel_scheduled': True})
        finally:
            event.remove(Session, 'after_commit', _after_commit)
        self.assertIn((0, 0, 0, 0), seen)
        self.assertEqual([s for s in seen if s not in (before, (0, 0, 0, 0))], [],
                         f'a commit showed the delete half done: {seen}')

    def test_cancel_scheduled_false_keeps_everything(self):
        resp = self.delete({'cancel_scheduled': False})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json(), {'success': True, 'cancelled': 0, 'kept': 3})
        rec = db.session.get(Recording, self.waiting.id)
        self.assertEqual((rec.status, rec.rule_id), (REC_STATUS_SCHEDULED, None))
        self.unscheduled.assert_not_called()
        self.assert_rule_gone()

    def test_the_choice_must_be_stated(self):
        """The delete never guesses what to do with the rule's scheduled recordings."""
        for body in ({}, {'cancel_scheduled': 'yes'}, {'cancel_scheduled': 1},
                     {'cancel_scheduled': None}):
            with self.subTest(body=body):
                resp = self.delete(body)
                self.assertEqual(resp.status_code, 400)
                self.assertTrue(resp.get_json().get('error'))
                self.assertIsNotNone(db.session.get(AutoRule, self.rid))
                self.assertEqual(len(self.recordings(self.rid)), 3)
                self.assertEqual(len(self.ledger(self.rid)), 4)
        self.unscheduled.assert_not_called()

    def test_deleting_the_last_enabled_rule_clears_the_guide_reach_alert(self):
        """The guide here reaches only hours ahead, so the alert stands while a rule is
        enabled; the delete re-checks it (§6, §8)."""
        source = 'auto-rules:guide-reach'
        self.assertEqual(len(self.open_alerts(AUTO_RULES_GUIDE_SHORT, source)), 1)
        self.t.client.delete(f'/api/auto-rules/{self.other_rid}',
                             json={'cancel_scheduled': False})
        db.session.expire_all()
        self.assertEqual(len(self.open_alerts(AUTO_RULES_GUIDE_SHORT, source)), 1)
        self.delete({'cancel_scheduled': False})
        self.assertEqual(self.open_alerts(AUTO_RULES_GUIDE_SHORT, source), [])


# ---------------------------------------------------------------------------
# §8 - a profile a rule uses is deleted
# ---------------------------------------------------------------------------

class ProfileDeleteTests(_LedgerCase):

    def test_a_deleted_profile_unlinks_the_rule_and_each_showing_takes_its_default(self):
        race = RecordingProfile(name='Race padding', pre_padding_minutes=5,
                                post_padding_minutes=30)
        channel_default = RecordingProfile(name='Channel default', pre_padding_minutes=2,
                                           post_padding_minutes=10)
        db.session.add_all([race, channel_default])
        db.session.commit()
        with_default = self.channel('Speed', default_profile_id=channel_default.id)
        bare = self.channel('Speed Two')
        first = _future(hours=4)
        self.showing(with_default, 'Cup Race', first, sub_title='Round 1')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=0, profile_id=race.id)
        self.evaluate(rid)
        (picked,) = self.recordings(rid)
        self.assertEqual((picked.profile_id, picked.start_time),
                         (race.id, first - timedelta(minutes=5)))

        page = self.t.client.get('/profiles')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'ruleCounts: {{"{race.id}": 1}}', page.get_data(as_text=True),
                      'the profile page knows how many rules use the profile')

        resp = self.t.client.delete(f'/api/profiles/{race.id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(AutoRule, rid).profile_id)

        second, third = _future(hours=8), _future(hours=12)
        self.showing(with_default, 'Cup Race', second, minutes=60, sub_title='Round 2')
        self.showing(bare, 'Cup Race', third, minutes=60, sub_title='Round 3')
        db.session.commit()
        out = self.evaluate(rid)
        self.assertFalse(out.get('error'), out)
        recs = {r.program_start_time: r for r in self.recordings(rid)}
        self.assertEqual((recs[second].profile_id, recs[second].start_time,
                          recs[second].stop_time),
                         (channel_default.id, second - timedelta(minutes=2),
                          second + timedelta(minutes=70)))
        self.assertEqual((recs[third].profile_id, recs[third].start_time,
                          recs[third].stop_time),
                         (None, third, third + timedelta(minutes=60)))
        self.assertEqual(self.broken_alerts(rid), [])


# ---------------------------------------------------------------------------
# §8 - pause and resume
# ---------------------------------------------------------------------------

class PauseResumeTests(_LedgerCase):

    def test_pause_stops_evaluation_and_resume_evaluates_at_once(self):
        ch = self.channel('Speed')
        starts = [_future(hours=4 + 4 * i) for i in range(4)]
        for i, start in enumerate(starts):
            self.showing(ch, 'Cup Race', start, sub_title=f'Round {i + 1}')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=2)
        self.evaluate(rid)
        self.assertEqual(len(self.cap_alerts(rid)), 1)
        create_alert(AUTO_RULE_BROKEN, title='broken', body='seeded',
                     source=f'auto-rule:{rid}:broken')
        guide = 'auto-rules:guide-reach'
        self.assertEqual(len(self.open_alerts(AUTO_RULES_GUIDE_SHORT, guide)), 1)

        resp = self.t.client.post(f'/api/auto-rules/{rid}/pause')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(resp.get_json()['rule']['enabled'])
        db.session.expire_all()
        rule = db.session.get(AutoRule, rid)
        self.assertEqual((rule.enabled, rule.revision), (False, 2))
        self.assertEqual(self.cap_alerts(rid), [])
        self.assertEqual(self.broken_alerts(rid), [])
        self.assertEqual(self.open_alerts(AUTO_RULES_GUIDE_SHORT, guide), [],
                         'no enabled rule is left, so the guide alert is re-checked away')
        recs = self.recordings(rid)
        self.assertEqual([r.status for r in recs], [REC_STATUS_SCHEDULED] * 2,
                         'what a paused rule scheduled is untouched')

        # Free the cap: an evaluating rule would now act, a paused one must not.
        self.set_status(recs[0].id, REC_STATUS_COMPLETED)
        before = self.snapshot(rid)
        evaluated_at = db.session.get(AutoRule, rid).last_evaluated_at
        self.assertNotIn(rid, self.sweep())
        self.assertEqual(self.evaluate(rid).get('scheduled', 0), 0)
        self.assertEqual(self.snapshot(rid), before)
        self.assertEqual(len(self.recordings(rid)), 2)
        self.assertEqual(db.session.get(AutoRule, rid).last_evaluated_at, evaluated_at)

        resp = self.t.client.post(f'/api/auto-rules/{rid}/resume')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body['rule']['enabled'])
        self.assertEqual(body['evaluation']['scheduled'], 1)
        self.assertEqual(body['evaluation']['skipped'], {RULE_MATCH_SKIPPED_CAP: 1})
        db.session.expire_all()
        self.assertEqual(db.session.get(AutoRule, rid).revision, 3)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], starts[:3])
        self.assertEqual(len(self.cap_alerts(rid)), 1,
                         'the condition still holds, so resuming raises the cap alert again')


if __name__ == '__main__':
    unittest.main()
