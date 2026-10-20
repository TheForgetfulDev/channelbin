"""Tier 2 - how an auto-record rule is evaluated (DESIGN-autoschedule.md §4.1-§4.5, §6, the
interleavings of §7 that do not need retrack, and the matching bullets of §11; built in
dev/changelog/1186).

  - CommitUnitTests (§4.4, §4.3): a recording, its RECORDING_RULE_SCHEDULED event and its
    `scheduled` ledger row land in one commit; a failure after the recording insert but
    before that commit leaves none of the three and names the showing in last_problems; the
    next evaluation judges the showing again; one rule raising does not stop the next.
  - AfterCommitFailureTests (§4.3): schedule_recording() raising on the first call and on the
    engine's one retry leaves the recording and its row, and says so on the recording, the
    row and the rule; a retry that succeeds leaves no such trace.
  - RevisionTests (§4.3, R12): an edit, a pause or a delete committed while a run is working
    on the rule turns every later write unit of that run - the create, the skip batch, the
    episode fill-in, the step-5 write - into a no-op, and raises no alert.
  - AdmissionTests (§4.2, R4, R10): what a run yields to, what yields to it, what a refused
    save and a refused queued or periodic run do, and that the ticket is always released.
  - TriggerTests / SchedulerTriggerTests (§4.1, §9): import_source() queues an evaluation
    exactly when it wrote to the guide; queueing does nothing with no live scheduler and
    coalesces on one job id with one; the sweep's interval floor; the sweep prunes history.
  - EvaluationFlowTests (§4.1, §4.3): paused rules are skipped, the save path evaluates one
    rule, started showings are dropped, a planner skip writes no row and is judged again,
    warnings never stop a create.
  - ReadinessTests (§4.2, R13): an index that stops being ready mid-search sends the search
    round again unindexed, and the first result is never acted on.
  - TooBroadTests (§4.3 step 2): more than 1,000 upcoming showings is a broken rule, never a
    silent truncation; exactly 1,000 runs.
  - GuideReachTests / GuideShortAlertTests / CappedAlertTests / BrokenAlertTests (§4.5, §6):
    the per-rule reach and the three self-clearing alerts, raised and cleared on their
    stated conditions.
  - InterleavingTests (§7 R1, R2, R6, R7, R8).
  - ScalingTests (§11): a re-evaluation of decided matches costs the same for 5 as for 400;
    a broad first evaluation writes its skips in one commit; scheduling costs a fixed amount
    per showing.
  - EnvelopeTests: the rules API's JSON envelope.

Every DB-backed test builds on make_test_app() and patches app.routes.recordings
.schedule_recording, so no scheduler job is ever armed except in SchedulerTriggerTests, which
asks for a live scheduler on its own temp jobstore. No network, no ffmpeg.

    python3 -m unittest tests.test_auto_rules_engine
"""
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

import app.config as config_mod  # noqa: E402
import app.scheduler as scheduler_mod  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from app import admission, auto_rules, channel_search, db, search_index  # noqa: E402
from app.alerts import (ALERT_TYPES, AUTO_RULE_BROKEN, AUTO_RULE_CAPPED,  # noqa: E402
                        AUTO_RULES_GUIDE_SHORT, RETIRED_ALERT_TYPES, is_self_clearing)
from app.database import (Alert, AutoRule, CANCEL_BEFORE_START, EPGEntry,  # noqa: E402
                          RECORDING_RULE_SCHEDULED, RECORDING_START_NOT_ARMED,
                          REC_STATUS_ABORTED, REC_STATUS_COMPLETED, REC_STATUS_FAILED,
                          REC_STATUS_SCHEDULED, RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_CAP,
                          RULE_MATCH_SKIPPED_EXISTING, RULE_MATCH_SKIPPED_REPEAT,
                          RULE_MATCH_SKIPS, Recording, RecordingEvent, RuleMatch)

_REAL_LOAD_CONFIG = config_mod.load_config


def _future(hours=0, minutes=0):
    """A minute-aligned naive-UTC moment ahead of now, so two values that must be equal (a
    seeded start and a stored reach) compare exactly."""
    return (datetime.utcnow() + timedelta(hours=hours, minutes=minutes)).replace(
        second=0, microsecond=0)


def _config_with(**auto_scheduling):
    """Patch app.config.load_config to answer the real config with `auto_scheduling` keys
    overridden - the engine and the scheduler import load_config inside their functions."""
    def fake(*args, **kwargs):
        cfg = copy.deepcopy(_REAL_LOAD_CONFIG(*args, **kwargs))
        cfg.setdefault('auto_scheduling', {}).update(auto_scheduling)
        return cfg
    return patch('app.config.load_config', side_effect=fake)


class _CommitLog:
    """What each committed transaction on this thread's session wrote: a list, one entry per
    commit, of (kind, detail) tuples - ('RuleMatch', verdict) and ('RecordingEvent', type)
    per insert, ('Recording', 'inserted') per new recording, and ('Recording.rule_id', id)
    whenever a flush writes a recording's rule_id, on its insert or on an update. A
    rolled-back transaction leaves no entry."""

    def __init__(self):
        self.commits = []
        self._pending = []

    def __enter__(self):
        self.session = db.session()
        event.listen(self.session, 'after_flush', self._flushed)
        event.listen(self.session, 'after_commit', self._committed)
        event.listen(self.session, 'after_rollback', self._rolled_back)
        return self

    def __exit__(self, *exc):
        event.remove(self.session, 'after_flush', self._flushed)
        event.remove(self.session, 'after_commit', self._committed)
        event.remove(self.session, 'after_rollback', self._rolled_back)
        return False

    def _flushed(self, session, _ctx):
        for obj in session.new:
            if isinstance(obj, RuleMatch):
                self._pending.append(('RuleMatch', obj.action_taken))
            elif isinstance(obj, RecordingEvent):
                self._pending.append(('RecordingEvent', obj.event_type))
            elif isinstance(obj, Recording):
                self._pending.append(('Recording', 'inserted'))
                if obj.rule_id is not None:
                    self._pending.append(('Recording.rule_id', obj.rule_id))
        for obj in session.dirty:
            if isinstance(obj, Recording) and obj.rule_id is not None:
                self._pending.append(('Recording.rule_id', obj.rule_id))

    def _committed(self, _session):
        self.commits.append(self._pending)
        self._pending = []

    def _rolled_back(self, _session):
        self._pending = []


# duplicated from tests/test_auto_rules_matching.py::_RuleCase - each rule suite owns its
# fixture so either can grow without reshaping the other's tests.
class _EngineCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.armed = patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(patch.stopall)
        self.acc = seed.make_account()
        self._next_stream = 100

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def channel(self, name, account=None, **kw):
        # Stream ids unique across accounts, so no two seeded channels share a stream URL.
        self._next_stream += 1
        return seed.make_channel(account or self.acc, stream_id=self._next_stream, name=name,
                                 **kw)

    def showing(self, channel, title, start, minutes=60, sub_title=None):
        return seed.make_epg_entry(channel, title=title, sub_title=sub_title,
                                   start_time=start, duration_minutes=minutes)

    def rule(self, search, **kw):
        data = {'name': kw.pop('name', 'Test rule'), 'search': search}
        data.update(kw)
        return auto_rules.create_rule(auto_rules.read_rule_values(data))

    def evaluate(self, rule_id):
        out = auto_rules.evaluate_now(rule_id)
        db.session.expire_all()
        return out

    def run_all(self, trigger=auto_rules.TRIGGER_SWEEP, rule_ids=None):
        out = auto_rules.run_evaluation(trigger, rule_ids)
        db.session.expire_all()
        return out

    def get_rule(self, rule_id):
        db.session.expire_all()
        return db.session.get(AutoRule, rule_id)

    def recordings(self, rule_id=None, title=None):
        q = Recording.query
        if rule_id is not None:
            q = q.filter_by(rule_id=rule_id)
        if title is not None:
            q = q.filter_by(program_title=title)
        return q.order_by(Recording.program_start_time, Recording.id).all()

    def ledger(self, rule_id):
        return (RuleMatch.query.filter_by(rule_id=rule_id)
                .order_by(RuleMatch.program_start_time, RuleMatch.id).all())

    def open_alerts(self, alert_type, source):
        db.session.expire_all()
        return Alert.query.filter_by(alert_type=alert_type, source=source,
                                     dismissed_at=None).all()

    def cap_alerts(self, rule_id):
        return self.open_alerts(AUTO_RULE_CAPPED, f'auto-rule:{rule_id}:cap')

    def broken_alerts(self, rule_id):
        return self.open_alerts(AUTO_RULE_BROKEN, f'auto-rule:{rule_id}:broken')

    def guide_alerts(self):
        return self.open_alerts(AUTO_RULES_GUIDE_SHORT, 'auto-rules:guide-reach')

    def far_filler(self):
        """A listing far ahead on a channel no test rule searches, so the guide-wide reach is
        long and the guide-reach alert stays out of a test that is not about it."""
        self.showing(self.channel('Far Filler Channel'), 'Zzfiller', _future(hours=200))


# ---------------------------------------------------------------------------
# §4.4 - one commit per recording; §4.3 - isolation
# ---------------------------------------------------------------------------

class CommitUnitTests(_EngineCase):

    def _one_showing(self, title='Cup Race One'):
        ch = self.channel('Speed')
        start = _future(hours=4)
        self.showing(ch, title, start)
        self.far_filler()
        db.session.commit()
        return ch, start

    def _assert_nothing_written(self, rid, title):
        self.assertEqual(self.recordings(title=title), [], 'no recording may survive')
        self.assertEqual(self.ledger(rid), [], 'no ledger row may survive')
        self.assertEqual(RecordingEvent.query.filter_by(
            event_type=RECORDING_RULE_SCHEDULED).count(), 0)
        rule = self.get_rule(rid)
        self.assertIn(title, rule.last_problems or '',
                      'last_problems must name the showing that could not be scheduled')
        self.assertIsNone(rule.last_error, 'the check ran, so the rule is not broken')

    def test_the_recording_its_event_and_its_ledger_row_land_in_one_commit(self):
        """§4.4: the Recording (with rule_id), its RECORDING_RULE_SCHEDULED event and its
        `scheduled` row (with rec_start_time/rec_stop_time) are one commit - no commit ever
        carries one of them without the other two."""
        self._one_showing()
        rid = self.rule('grain=airings&q=Cup+Race')
        with _CommitLog() as log:
            out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)

        carrying = [c for c in log.commits
                    if any(kind in ('Recording', 'Recording.rule_id', 'RuleMatch')
                           for kind, _ in c)
                    or ('RecordingEvent', RECORDING_RULE_SCHEDULED) in c]
        self.assertEqual(len(carrying), 1, f'expected one commit, got {carrying}')
        self.assertIn(('Recording', 'inserted'), carrying[0])
        self.assertIn(('Recording.rule_id', rid), carrying[0])
        self.assertIn(('RecordingEvent', RECORDING_RULE_SCHEDULED), carrying[0])
        self.assertIn(('RuleMatch', RULE_MATCH_SCHEDULED), carrying[0])

        rec = self.recordings(rid)[0]
        row = self.ledger(rid)[0]
        self.assertEqual(row.recording_id, rec.id)
        self.assertEqual((row.rec_start_time, row.rec_stop_time), (rec.start_time, rec.stop_time))
        event_row = RecordingEvent.query.filter_by(recording_id=rec.id,
                                                   event_type=RECORDING_RULE_SCHEDULED).one()
        self.assertEqual(json.loads(event_row.extra_data)['rule_id'], rid)
        self.assertIn('Test rule', event_row.detail)
        self.assertIn('Cup Race One', event_row.detail)

    def test_a_failure_at_the_ledger_insert_leaves_neither_the_recording_nor_its_row(self):
        """§4.4: the Recording is already flushed when its ledger row's insert fails; the
        whole unit rolls back, and the next evaluation judges the showing again."""
        self._one_showing()
        rid = self.rule('grain=airings&q=Cup+Race')
        fired = []

        def refuse(_mapper, _conn, target):
            if target.action_taken == RULE_MATCH_SCHEDULED and not fired:
                fired.append(target)
                raise RuntimeError('ledger write failed')

        event.listen(RuleMatch, 'before_insert', refuse)
        try:
            out = self.evaluate(rid)
        finally:
            event.remove(RuleMatch, 'before_insert', refuse)
        self.assertTrue(fired, 'the injected failure never ran')
        self.assertEqual(out['scheduled'], 0)
        self._assert_nothing_written(rid, 'Cup Race One')

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(len(self.recordings(rid)), 1)
        self.assertIsNone(self.get_rule(rid).last_problems,
                          'a clean evaluation clears last_problems')

    def test_a_failure_at_the_commit_leaves_neither_the_recording_nor_its_row(self):
        """§4.4: everything the create writes is flushed and the commit itself fails - still
        no recording, no event, no ledger row."""
        self._one_showing()
        rid = self.rule('grain=airings&q=Cup+Race')
        session = db.session()
        state = {'armed': False, 'fired': False}

        def note(sess, _ctx):
            if any(isinstance(o, RuleMatch) and o.action_taken == RULE_MATCH_SCHEDULED
                   for o in sess.new):
                state['armed'] = True

        def refuse(_sess):
            if state['armed'] and not state['fired']:
                state['fired'] = True
                raise RuntimeError('commit failed')

        event.listen(session, 'after_flush', note)
        event.listen(session, 'before_commit', refuse)
        try:
            self.evaluate(rid)
        finally:
            event.remove(session, 'after_flush', note)
            event.remove(session, 'before_commit', refuse)
        self.assertTrue(state['fired'], 'the injected failure never ran')
        self._assert_nothing_written(rid, 'Cup Race One')

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertIsNone(self.get_rule(rid).last_problems)

    def test_a_create_that_raises_names_the_showing_and_the_rest_are_still_scheduled(self):
        """§4.3: each create is caught on its own. A raise inside the create closure leaves no
        recording and no row for that showing, names it in last_problems, and does not stop the
        rule's other showings; the next evaluation schedules it and clears last_problems."""
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.showing(ch, 'Cup Race Two', _future(hours=10))
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        real = auto_rules.add_recording_event
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('event table unavailable')
            return real(*args, **kwargs)

        with patch('app.auto_rules.add_recording_event', side_effect=flaky):
            out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual([r.program_title for r in self.recordings(rid)], ['Cup Race Two'])
        self.assertEqual([m.program_title for m in self.ledger(rid)], ['Cup Race Two'])
        rule = self.get_rule(rid)
        self.assertIn('Cup Race One', rule.last_problems or '')
        self.assertNotIn('Cup Race Two', rule.last_problems or '')

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(sorted(r.program_title for r in self.recordings(rid)),
                         ['Cup Race One', 'Cup Race Two'])
        self.assertIsNone(self.get_rule(rid).last_problems)

    def test_one_rule_raising_does_not_stop_the_next(self):
        """§4.3: each rule is evaluated under its own catch - the one that raises gets a
        last_error, the run moves on, and the admission ticket is still released."""
        ch = self.channel('Speed')
        self.showing(ch, 'Alpha Show', _future(hours=3))
        self.showing(ch, 'Beta Show', _future(hours=6))
        self.far_filler()
        db.session.commit()
        alpha = self.rule('grain=airings&q=Alpha+Show', name='alpha')
        beta = self.rule('grain=airings&q=Beta+Show', name='beta')
        self.assertLess(alpha, beta, 'the failing rule must be evaluated first')
        real_search = channel_search.search

        def search(state, *args, **kwargs):
            if state.q and 'Alpha' in state.q:
                raise RuntimeError('search exploded')
            return real_search(state, *args, **kwargs)

        with patch('app.channel_search.search', side_effect=search):
            outcomes = self.run_all()
        self.assertTrue(outcomes[alpha].stopped)
        self.assertEqual(outcomes[beta].scheduled, 1)
        self.assertEqual(self.recordings(alpha), [])
        self.assertEqual(len(self.recordings(beta)), 1)
        self.assertIn('search exploded', self.get_rule(alpha).last_error or '')
        self.assertIsNone(self.get_rule(beta).last_error)
        self.assertEqual(admission.active_kinds(), set())


# ---------------------------------------------------------------------------
# §4.3 - a raise after the commit
# ---------------------------------------------------------------------------

class AfterCommitFailureTests(_EngineCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.far_filler()
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race')

    def test_schedule_recording_failing_twice_keeps_the_recording_and_says_so(self):
        """The recording and its `scheduled` row stay; a RECORDING_START_NOT_ARMED event on
        the recording, the row's detail and the rule's last_problems carry the same sentence;
        the showing is never judged again, and the event and detail outlive the next clean
        run's cleared last_problems."""
        self.armed.side_effect = [RuntimeError('jobstore unavailable'),
                                  RuntimeError('jobstore unavailable')]
        out = self.evaluate(self.rid)
        self.assertEqual(self.armed.call_count, 2, 'one call by the create, one retry')
        self.assertEqual(out['scheduled'], 1)

        recs = self.recordings(self.rid)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].status, REC_STATUS_SCHEDULED)
        rows = self.ledger(self.rid)
        self.assertEqual([(m.action_taken, m.recording_id) for m in rows],
                         [(RULE_MATCH_SCHEDULED, recs[0].id)])
        events = RecordingEvent.query.filter_by(recording_id=recs[0].id,
                                                event_type=RECORDING_START_NOT_ARMED).all()
        self.assertEqual(len(events), 1)
        sentence = events[0].detail
        self.assertTrue(sentence)
        self.assertIn(sentence, rows[0].detail or '')
        self.assertIn(sentence, self.get_rule(self.rid).last_problems or '')

        self.armed.side_effect = None
        self.evaluate(self.rid)
        self.assertEqual(len(self.recordings(self.rid)), 1, 'never judged again')
        self.assertIsNone(self.get_rule(self.rid).last_problems)
        self.assertIn(sentence, self.ledger(self.rid)[0].detail or '')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=recs[0].id, event_type=RECORDING_START_NOT_ARMED).count(), 1)

    def test_a_retry_that_succeeds_leaves_no_trace(self):
        self.armed.side_effect = [RuntimeError('jobstore unavailable'), None]
        out = self.evaluate(self.rid)
        self.assertEqual(self.armed.call_count, 2)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(RecordingEvent.query.filter_by(
            event_type=RECORDING_START_NOT_ARMED).count(), 0)
        self.assertIsNone(self.get_rule(self.rid).last_problems)
        self.assertEqual(len(self.recordings(self.rid)), 1)


# ---------------------------------------------------------------------------
# §4.3, R12 - every write re-checks the rule
# ---------------------------------------------------------------------------

class RevisionTests(_EngineCase):

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.far_filler()
        db.session.commit()

    def _rule_evaluated_once(self, **kw):
        """A rule with a clean evaluation behind it, so last_evaluated_at has a value a later
        abandoned run must leave alone."""
        rid = self.rule('grain=airings&q=Cup+Race', **kw)
        self.evaluate(rid)
        before = self.get_rule(rid).last_evaluated_at
        self.assertIsNotNone(before)
        return rid, before

    def _edit(self, rid):
        rule = db.session.get(AutoRule, rid)
        auto_rules.update_rule(rid, auto_rules.read_rule_values({'name': 'renamed'},
                                                                existing=rule))

    def _pause(self, rid):
        auto_rules.set_rule_enabled(rid, False)

    def _delete(self, rid):
        auto_rules.delete_rule(rid, cancel_scheduled=False)

    def _assert_create_is_a_no_op(self, change):
        rid, before = self._rule_evaluated_once()
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        db.session.commit()
        from app.routes import recordings as rec_routes
        real_plan = rec_routes._plan_bulk_schedule
        changed = []

        def plan(*args, **kwargs):
            if not changed:
                changed.append(1)
                change(rid)
            return real_plan(*args, **kwargs)

        with patch('app.routes.recordings._plan_bulk_schedule', side_effect=plan):
            outcome = self.run_all(auto_rules.TRIGGER_QUEUED)
        self.assertTrue(changed, 'the planner was never reached')
        self.assertTrue(outcome[rid].abandoned)
        self.assertEqual(self.recordings(title='Cup Race One'), [])
        self.assertEqual(Recording.query.count(), 0)
        self.assertEqual(self.ledger(rid), [])
        rule = self.get_rule(rid)
        if rule is not None:
            self.assertEqual(rule.last_evaluated_at, before)
        self.assertEqual(self.cap_alerts(rid), [])
        self.assertEqual(self.broken_alerts(rid), [])

    def test_an_edit_mid_run_makes_the_create_a_no_op(self):
        self._assert_create_is_a_no_op(self._edit)

    def test_a_pause_mid_run_makes_the_create_a_no_op(self):
        self._assert_create_is_a_no_op(self._pause)

    def test_a_delete_mid_run_makes_the_create_a_no_op(self):
        self._assert_create_is_a_no_op(self._delete)

    def test_a_pause_mid_run_makes_the_skip_batch_a_no_op_and_raises_no_cap_alert(self):
        """The skip verdicts are one revision-checked commit (§4.4): a pause after they were
        judged writes none of them, and the cap alert is raised only after the same check."""
        rid, before = self._rule_evaluated_once(max_upcoming=1)
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        self.showing(self.ch, 'Cup Race Two', _future(hours=10))
        db.session.commit()
        real_verdict = auto_rules._Judge.verdict
        paused = []

        def verdict(judge, program):
            out = real_verdict(judge, program)
            if out[0] == RULE_MATCH_SKIPPED_CAP and not paused:
                paused.append(1)
                self._pause(rid)
            return out

        with patch.object(auto_rules._Judge, 'verdict', verdict):
            outcome = self.run_all(auto_rules.TRIGGER_QUEUED)
        self.assertTrue(paused, 'no showing reached the cap')
        self.assertTrue(outcome[rid].abandoned)
        self.assertEqual([m.action_taken for m in self.ledger(rid)], [RULE_MATCH_SCHEDULED],
                         'the pick committed before the pause stays; the cap skip is not written')
        self.assertEqual(self.get_rule(rid).last_evaluated_at, before)
        self.assertEqual(self.cap_alerts(rid), [])

    def test_a_pause_mid_run_makes_the_final_write_a_no_op_and_raises_no_cap_alert(self):
        """The step-5 write (last_evaluated_at, last_guide_reach, last_error) and the rule's
        alerts are behind the same check."""
        rid, before = self._rule_evaluated_once(max_upcoming=1)
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        self.showing(self.ch, 'Cup Race Two', _future(hours=10))
        db.session.commit()
        real_reach = auto_rules._guide_reach_for_rule

        def reach(rule_id, *args, **kwargs):
            if rule_id == rid:
                self._pause(rid)
            return real_reach(rule_id, *args, **kwargs)

        with patch('app.auto_rules._guide_reach_for_rule', side_effect=reach):
            outcome = self.run_all(auto_rules.TRIGGER_QUEUED)
        self.assertTrue(outcome[rid].abandoned)
        self.assertEqual(self.get_rule(rid).last_evaluated_at, before)
        self.assertEqual(self.cap_alerts(rid), [])

    def test_a_pause_mid_run_makes_the_episode_fill_in_a_no_op(self):
        """The episode fill-in (§3.3) is a write like any other: a pause noted after the rule
        was read leaves the empty episode as it was."""
        start = _future(hours=4)
        self.showing(self.ch, 'Cup Race', start)
        db.session.commit()
        rid, before = self._rule_evaluated_once()
        self.assertEqual([m.episode_key for m in self.ledger(rid)], [''])
        self.showing(self.channel('Speed Two'), 'Cup Race', start, sub_title='Daytona 500')
        db.session.commit()
        real_snapshot = auto_rules._row_snapshot
        paused = []

        def snapshot(m):
            if not paused:
                paused.append(1)
                self._pause(rid)
            return real_snapshot(m)

        with patch('app.auto_rules._row_snapshot', side_effect=snapshot):
            outcome = self.run_all(auto_rules.TRIGGER_QUEUED)
        self.assertTrue(paused)
        self.assertTrue(outcome[rid].abandoned)
        self.assertEqual([m.episode_key for m in self.ledger(rid)], [''])
        self.assertEqual(self.get_rule(rid).last_evaluated_at, before)


# ---------------------------------------------------------------------------
# §4.2 - admission
# ---------------------------------------------------------------------------

class AdmissionTests(_EngineCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.far_filler()
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race')

    def test_rules_yield_to_exactly_sync_rebuild_maintenance_and_themselves(self):
        self.assertEqual(set(admission.BLOCKED_BY[admission.KIND_RULES]),
                         {admission.KIND_SYNC, admission.KIND_REBUILD,
                          admission.KIND_MAINTENANCE, admission.KIND_RULES})

    def test_each_blocker_refuses_a_run_and_nothing_is_evaluated(self):
        for kind in (admission.KIND_SYNC, admission.KIND_REBUILD,
                     admission.KIND_MAINTENANCE, admission.KIND_RULES):
            with self.subTest(kind=kind):
                held = admission.try_start(kind, 'held by the test')
                self.assertTrue(held.granted)
                try:
                    out = auto_rules.run_evaluation(auto_rules.TRIGGER_SWEEP)
                finally:
                    admission.release(held)
                self.assertIsInstance(out, admission.Refusal)
                self.assertEqual(out.blocked_by, kind)
                self.assertTrue(out.reason)
                self.assertEqual(self.recordings(self.rid), [])
                self.assertIsNone(self.get_rule(self.rid).last_evaluated_at)
                self.assertEqual(admission.active_kinds(), set())

    def test_a_health_check_run_does_not_hold_a_run_back(self):
        held = admission.try_start(admission.KIND_TESTER, 'a test run')
        try:
            out = self.run_all()
        finally:
            admission.release(held)
        self.assertNotIsInstance(out, admission.Refusal)
        self.assertEqual(out[self.rid].scheduled, 1)

    def test_a_refused_save_queues_and_says_what_it_waits_behind(self):
        held = admission.try_start(admission.KIND_SYNC, 'account 3')
        try:
            with patch('app.auto_rules.queue_evaluation') as queue:
                out = auto_rules.evaluate_now(self.rid)
        finally:
            admission.release(held)
        self.assertTrue(out['queued'])
        self.assertIn('account sync', out['waiting_for'])
        self.assertIn('account 3', out['waiting_for'])
        queue.assert_called_once()
        self.assertEqual(self.recordings(self.rid), [])

    def test_the_save_route_answers_a_refusal_with_what_it_waits_behind(self):
        held = admission.try_start(admission.KIND_MAINTENANCE, 'daily maintenance')
        try:
            with patch('app.auto_rules.queue_evaluation') as queue:
                resp = self.t.client.post('/api/auto-rules', json={
                    'name': 'queued', 'search': 'grain=airings&q=Cup+Race'})
        finally:
            admission.release(held)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertTrue(body['evaluation']['queued'])
        self.assertIn('database maintenance', body['evaluation']['waiting_for'])
        queue.assert_called_once()

    def test_a_refused_queued_or_periodic_run_requeues_itself_a_few_minutes_later(self):
        for kind in ('queued', 'sweep'):
            with self.subTest(kind=kind):
                held = admission.try_start(admission.KIND_SYNC, 'account 3')
                try:
                    with patch('app.scheduler.queue_auto_rule_evaluation') as queue:
                        ran = scheduler_mod._run_auto_rules(kind)
                finally:
                    admission.release(held)
                self.assertFalse(ran)
                queue.assert_called_once()
                delay = queue.call_args.kwargs.get('delay_seconds')
                self.assertTrue(60 <= delay <= 15 * 60, f'not a few minutes: {delay}s')
                self.assertIn('account sync', queue.call_args.kwargs.get('reason', ''))

    def test_a_granted_queued_run_does_not_requeue(self):
        with patch('app.scheduler.queue_auto_rule_evaluation') as queue:
            ran = scheduler_mod._run_auto_rules('queued')
        self.assertTrue(ran)
        queue.assert_not_called()
        db.session.expire_all()
        self.assertEqual(len(self.recordings(self.rid)), 1)

    def test_maintenance_and_the_janitor_yield_to_a_running_evaluation_and_sync_does_not(self):
        """Probed from inside a real run: while the evaluation holds its ticket, maintenance
        and the refusable rebuild (the index janitor) are refused, a sync is not (R10), and a
        forced rebuild still registers (R13)."""
        probes = {}
        real_search = channel_search.search

        def probing(*args, **kwargs):
            if not probes:
                for kind in (admission.KIND_MAINTENANCE, admission.KIND_SYNC):
                    ticket = admission.try_start(kind, 'probe')
                    probes[kind] = ticket.granted
                    admission.release(ticket)
                forced = admission.try_start(admission.KIND_REBUILD, 'manual', force=True)
                probes['forced rebuild'] = forced.granted
                admission.release(forced)
                probes['janitor'] = search_index.rebuild_search_indexes('index janitor',
                                                                        refusable=True)
            return real_search(*args, **kwargs)

        with patch('app.channel_search.search', side_effect=probing):
            self.run_all()
        self.assertEqual(probes, {admission.KIND_MAINTENANCE: False, admission.KIND_SYNC: True,
                                  'forced rebuild': True, 'janitor': {}})

    def test_the_ticket_is_released_after_a_run_and_after_one_that_raised(self):
        self.run_all()
        self.assertEqual(admission.active_kinds(), set())
        with patch('app.channel_search.SearchContext.build',
                   side_effect=RuntimeError('context failed')):
            with self.assertRaises(RuntimeError):
                auto_rules.run_evaluation(auto_rules.TRIGGER_SWEEP)
        self.assertEqual(admission.active_kinds(), set())
        held = admission.try_start(admission.KIND_RULES, 'next run')
        self.assertTrue(held.granted, 'a leaked ticket would refuse the next run')
        admission.release(held)


# ---------------------------------------------------------------------------
# §4.1 - triggers
# ---------------------------------------------------------------------------

def _xmltv(channel_key, starts, title='Show'):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for start in starts:
        stop = start + timedelta(minutes=30)
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                     f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{channel_key}">'
                     f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class TriggerTests(_EngineCase):

    def setUp(self):
        super().setUp()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch1')
        self.source = seed.make_epg_source(self.acc)
        db.session.commit()
        self.cfg = {'sync': {'epg_collapse_threshold_percent': 20}}

    def _import(self, xml):
        from app.accounts import import_source
        with patch('app.auto_rules.queue_evaluation') as queue:
            synced, reason = import_source(self.source, xml, epg_days=3, cfg=self.cfg)
        return synced, reason, queue

    def test_an_import_that_wrote_to_the_guide_queues_an_evaluation(self):
        synced, reason, queue = self._import(_xmltv(self.ch.epg_channel_id,
                                                    [_future(hours=2), _future(hours=5)]))
        self.assertIsNone(reason)
        self.assertEqual(synced, 2)
        queue.assert_called_once()

    def test_a_refused_import_queues_nothing(self):
        for i in range(100):
            self.showing(self.ch, f'Old {i}', _future(hours=1 + i))
        db.session.commit()
        synced, reason, queue = self._import(_xmltv(self.ch.epg_channel_id, [_future(hours=2)]))
        self.assertTrue((reason or '').startswith('import refused:'), reason)
        queue.assert_not_called()

    def test_an_undecodable_import_queues_nothing(self):
        _synced, reason, queue = self._import(b'\x1f\x8b' + b'not really gzip at all')
        self.assertTrue(reason)
        queue.assert_not_called()

    def test_queueing_with_no_live_scheduler_arms_nothing(self):
        self.assertFalse(scheduler_mod.scheduler_is_live())
        self.assertFalse(scheduler_mod.queue_auto_rule_evaluation())
        self.assertFalse(auto_rules.queue_evaluation('a test'))

    def test_the_sweep_prunes_history_older_than_the_keep_window(self):
        rid = self.rule('grain=airings&q=Nothing+Here')
        now = datetime.utcnow()
        for days, title in ((100, 'old'), (10, 'recent')):
            start = now - timedelta(days=days)
            db.session.add(RuleMatch(rule_id=rid, title_key=title, episode_key='',
                                     program_start_time=start,
                                     program_stop_time=start + timedelta(hours=1),
                                     action_taken=RULE_MATCH_SKIPPED_REPEAT))
        db.session.commit()

        with _config_with(history_keep_days=0):
            self.assertTrue(scheduler_mod._run_auto_rules('sweep'))
        db.session.expire_all()
        self.assertEqual(sorted(m.title_key for m in self.ledger(rid)), ['old', 'recent'],
                         '0 keeps everything')

        with _config_with(history_keep_days=90):
            self.assertTrue(scheduler_mod._run_auto_rules('sweep'))
        db.session.expire_all()
        self.assertEqual([m.title_key for m in self.ledger(rid)], ['recent'])


class SchedulerTriggerTests(unittest.TestCase):
    """The queued evaluation and the sweep on a real scheduler, against its own temp
    jobstore."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def _job(self, job_id):
        return scheduler_mod._scheduler.get_job(job_id)

    def test_queueing_coalesces_on_one_job(self):
        self.assertTrue(scheduler_mod.queue_auto_rule_evaluation(delay_seconds=600,
                                                                 reason='first import'))
        first = self._job(scheduler_mod.AUTO_RULES_EVALUATE_JOB_ID)
        self.assertIsNotNone(first)
        self.assertTrue(scheduler_mod.queue_auto_rule_evaluation(delay_seconds=900,
                                                                 reason='second import'))
        ids = [j.id for j in scheduler_mod._scheduler.get_jobs()
               if j.id == scheduler_mod.AUTO_RULES_EVALUATE_JOB_ID]
        self.assertEqual(ids, ['auto_rules_evaluate'])
        second = self._job('auto_rules_evaluate')
        self.assertGreater(second.next_run_time, first.next_run_time,
                           'the later ask replaces the earlier one')

    def test_the_sweep_is_registered_at_startup_hourly(self):
        job = self._job('auto_rule_sweep')
        self.assertIsNotNone(job, 'init_scheduler() must register the sweep')
        self.assertEqual(job.trigger.interval, timedelta(hours=1))

    def test_the_sweep_interval_has_a_floor_of_one_hour(self):
        for raw, hours in ((3, 3), (0, 1), (5, 5), ('abc', 1), (-4, 1)):
            with self.subTest(sweep_interval_hours=raw):
                with _config_with(sweep_interval_hours=raw):
                    scheduler_mod.schedule_auto_rule_sweep(self.t.app)
                job = self._job('auto_rule_sweep')
                self.assertEqual(job.trigger.interval, timedelta(hours=hours))


# ---------------------------------------------------------------------------
# §4.1, §4.3 - the run itself
# ---------------------------------------------------------------------------

class EvaluationFlowTests(_EngineCase):

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.far_filler()
        db.session.commit()

    def test_a_paused_rule_is_not_evaluated(self):
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        auto_rules.set_rule_enabled(rid, False)
        out = self.run_all()
        self.assertNotIn(rid, out)
        self.assertEqual(self.recordings(rid), [])
        self.assertIsNone(self.get_rule(rid).last_evaluated_at)

    def test_the_save_path_evaluates_only_that_rule(self):
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        self.showing(self.ch, 'Golf Open', _future(hours=8))
        db.session.commit()
        cup = self.rule('grain=airings&q=Cup+Race', name='cup')
        golf = self.rule('grain=airings&q=Golf+Open', name='golf')
        out = self.evaluate(cup)
        self.assertFalse(out['queued'])
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(len(self.recordings(cup)), 1)
        self.assertEqual(self.recordings(golf), [])
        self.assertIsNone(self.get_rule(golf).last_evaluated_at)

    def test_resuming_evaluates_at_once(self):
        rid = self.rule('grain=airings&q=Cup+Race')
        resp = self.t.client.post(f'/api/auto-rules/{rid}/pause')
        self.assertEqual(resp.status_code, 200)
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        db.session.commit()
        resp = self.t.client.post(f'/api/auto-rules/{rid}/resume')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['evaluation']['scheduled'], 1)
        db.session.expire_all()
        self.assertEqual(len(self.recordings(rid)), 1)

    def test_a_showing_that_has_started_is_never_joined(self):
        """§3.1, §4.3 step 2: the search still lists a program that is on now; the rule drops
        it - no recording and no ledger row."""
        self.showing(self.ch, 'Cup Race Live', _future(minutes=-20), minutes=120)
        self.showing(self.ch, 'Cup Race Later', _future(hours=6))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        self.evaluate(rid)
        self.assertEqual([r.program_title for r in self.recordings(rid)], ['Cup Race Later'])
        self.assertEqual([m.program_title for m in self.ledger(rid)], ['Cup Race Later'])

    def test_a_planner_skip_writes_no_row_names_the_reason_and_is_judged_again(self):
        """§4.3 step 6: the guide changing under the run is not a verdict on the showing."""
        start = _future(hours=4)
        entry = self.showing(self.ch, 'Cup Race One', start)
        db.session.commit()
        entry_id = entry.id
        rid = self.rule('grain=airings&q=Cup+Race')
        from app.routes import recordings as rec_routes
        real_plan = rec_routes._plan_bulk_schedule
        removed = []

        def plan(*args, **kwargs):
            if not removed:
                removed.append(1)
                db.session.delete(db.session.get(EPGEntry, entry_id))
                db.session.commit()
            return real_plan(*args, **kwargs)

        with patch('app.routes.recordings._plan_bulk_schedule', side_effect=plan):
            out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual(self.recordings(rid), [])
        self.assertEqual(self.ledger(rid), [], 'a planner skip is never a ledger verdict')
        error = self.get_rule(rid).last_problems or ''
        self.assertIn('Cup Race One', error)
        self.assertIn('no longer in the guide data', error)

        self.showing(self.ch, 'Cup Race One', start)
        db.session.commit()
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertIsNone(self.get_rule(rid).last_problems)

    def test_a_warning_never_stops_the_create(self):
        """§4.3 step 7: an overlap is scheduled anyway, with the warning in the row's detail
        and on the recording's RECORDING_RULE_SCHEDULED event."""
        start = _future(hours=4)
        other = self.channel('News')
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='by hand', channel_id=other.id,
                            start_time=start - timedelta(minutes=30),
                            stop_time=start + timedelta(minutes=30))
        self.showing(self.ch, 'Cup Race One', start)
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        rec = self.recordings(rid)[0]
        row = self.ledger(rid)[0]
        self.assertEqual(row.action_taken, RULE_MATCH_SCHEDULED)
        self.assertIn('Overlaps 1 other scheduled recording', row.detail or '')
        ev = RecordingEvent.query.filter_by(recording_id=rec.id,
                                            event_type=RECORDING_RULE_SCHEDULED).one()
        self.assertIn('Overlaps 1 other scheduled recording', ev.detail)
        self.assertIn('overlap_warning', json.loads(ev.extra_data)['warnings'])


# ---------------------------------------------------------------------------
# §4.2, R13 - readiness re-read around each search
# ---------------------------------------------------------------------------

class ReadinessTests(_EngineCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Real Show', _future(hours=4))
        self.decoy = self.showing(self.channel('Elsewhere'), 'Decoy Program', _future(hours=5))
        self.far_filler()
        db.session.commit()
        self.decoy_id = self.decoy.id
        self.rid = self.rule('grain=airings&q=Real+Show')
        self.ready = {names: (True, '') for names in search_index.READINESS_SETS}
        self.not_ready = {names: (False, 'a rebuild is running')
                          for names in search_index.READINESS_SETS}

    def test_a_readiness_change_mid_search_reruns_it_unindexed_and_drops_the_first_result(self):
        reads = []

        def readiness():
            reads.append(1)
            return dict(self.ready if len(reads) == 1 else self.not_ready)

        real_search = channel_search.search
        seen = []

        def search(state, ctx, *args, **kwargs):
            seen.append(dict(ctx.readiness))
            if len(seen) == 1:
                # What an index stopping mid-search can hand back: something the rule's
                # search does not match.
                return SimpleNamespace(rows=[db.session.get(EPGEntry, self.decoy_id)],
                                       airing_group_ids={})
            return real_search(state, ctx, *args, **kwargs)

        with patch('app.search_index.readiness_map', side_effect=readiness), \
                patch('app.channel_search.search', side_effect=search):
            out = self.evaluate(self.rid)
        self.assertEqual(len(seen), 2, 'the search must run again')
        self.assertTrue(all(ready for ready, _r in seen[0].values()))
        self.assertTrue(all(not ready for ready, _r in seen[1].values()),
                        'the second search runs on the unindexed path')
        self.assertGreaterEqual(len(reads), 4, 'readiness is read before and after each search')
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual([r.program_title for r in self.recordings(self.rid)], ['Real Show'])
        self.assertEqual([m.program_title for m in self.ledger(self.rid)], ['Real Show'])

    def test_unchanged_readiness_searches_once(self):
        real_search = channel_search.search
        calls = []

        def search(*args, **kwargs):
            calls.append(1)
            return real_search(*args, **kwargs)

        with patch('app.search_index.readiness_map', side_effect=lambda: dict(self.not_ready)), \
                patch('app.channel_search.search', side_effect=search):
            out = self.evaluate(self.rid)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out['scheduled'], 1)


# ---------------------------------------------------------------------------
# §4.3 step 2 - too broad
# ---------------------------------------------------------------------------

class TooBroadTests(_EngineCase):

    def _seed(self, n):
        ch = self.channel('Marathon')
        base = _future(hours=1)
        db.session.execute(EPGEntry.__table__.insert(), [
            {'channel_id': ch.id, 'title': 'Broadcast Marathon',
             'start_time': base + timedelta(minutes=15 * i),
             'stop_time': base + timedelta(minutes=15 * i + 15)} for i in range(n)])
        db.session.commit()

    def test_more_than_a_thousand_upcoming_showings_is_not_run(self):
        self._seed(1001)
        rid = self.rule('grain=airings&q=Broadcast+Marathon')
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 0)
        self.assertEqual(self.recordings(rid), [])
        self.assertEqual(self.ledger(rid), [])
        rule = self.get_rule(rid)
        self.assertIn('too broad', rule.last_error or '')
        self.assertIn('too broad', out['error'] or '')
        self.assertEqual(len(self.broken_alerts(rid)), 1)

    def test_exactly_a_thousand_is_run(self):
        self._seed(1000)
        rid = self.rule('grain=airings&q=Broadcast+Marathon', max_upcoming=3)
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 3)
        self.assertEqual(out['skipped'], {RULE_MATCH_SKIPPED_CAP: 997})
        self.assertEqual(self.broken_alerts(rid), [])
        self.assertEqual(len(self.ledger(rid)), 1000)


# ---------------------------------------------------------------------------
# §4.5 - guide reach; §6 - AUTO_RULES_GUIDE_SHORT
# ---------------------------------------------------------------------------

class _SourcesCase(_EngineCase):
    """Two accounts, each with its own EPG source winning its channel: A reaches 30 hours
    ahead (fresh), B 10 hours (drained)."""

    def setUp(self):
        super().setUp()
        self.acc_b = seed.make_account(name='Second Provider')
        self.src_a = seed.make_epg_source(self.acc, name='Fresh Source')
        self.src_b = seed.make_epg_source(self.acc_b, name='Drained Source')
        self.ch_a = self.channel('Channel A', epg_source_id=self.src_a.id)
        self.ch_b = self.channel('Channel B', account=self.acc_b, epg_source_id=self.src_b.id)
        self.reach_a = _future(hours=30)
        self.reach_b = _future(hours=10)
        self.showing(self.ch_a, 'Pick Alpha', _future(hours=2))
        self.showing(self.ch_a, 'Filler Alpha', self.reach_a)
        self.showing(self.ch_b, 'Pick Bravo', _future(hours=3))
        self.showing(self.ch_b, 'Filler Bravo', self.reach_b)
        db.session.commit()


class GuideReachTests(_SourcesCase):

    def test_a_rules_reach_is_the_shortest_among_the_sources_its_matches_draw_from(self):
        rid = self.rule('grain=airings&q=Pick')
        self.evaluate(rid)
        self.assertEqual({m.channel_id for m in self.ledger(rid)}, {self.ch_a.id, self.ch_b.id})
        self.assertEqual(self.get_rule(rid).last_guide_reach, self.reach_b)

    def test_a_rule_drawing_only_from_the_fresh_source_reaches_as_far_as_it(self):
        rid = self.rule('grain=airings&q=Pick+Alpha')
        out = self.evaluate(rid)
        self.assertEqual(self.get_rule(rid).last_guide_reach, self.reach_a)
        self.assertEqual(out['guide_reach'], self.reach_a.strftime('%Y-%m-%dT%H:%M:%S'))

    def test_a_rule_with_no_matches_uses_the_guide_wide_figure(self):
        rid = self.rule('grain=airings&q=Nothing+Like+This')
        self.evaluate(rid)
        self.assertEqual(self.get_rule(rid).last_guide_reach, self.reach_a)

    def test_a_group_match_reaches_as_far_as_its_longest_reaching_member(self):
        acc_c = seed.make_account(name='Third Provider')
        src_c = seed.make_epg_source(acc_c, name='Group Short Source')
        acc_d = seed.make_account(name='Fourth Provider')
        src_d = seed.make_epg_source(acc_d, name='Group Long Source')
        m1 = self.channel('Member One', account=acc_c, epg_source_id=src_c.id)
        m2 = self.channel('Member Two', account=acc_d, epg_source_id=src_d.id)
        group = seed.make_group(name='Reach Group', members=[m1, m2])
        self.showing(m1, 'Grouped Pick', _future(hours=2))
        self.showing(m1, 'Member One Filler', _future(hours=5))
        long_reach = _future(hours=40)
        self.showing(m2, 'Member Two Filler', long_reach)
        db.session.commit()
        rid = self.rule('grain=airings&q=Grouped+Pick')
        self.evaluate(rid)
        self.assertEqual([m.group_id for m in self.ledger(rid)], [group.id])
        self.assertEqual(self.get_rule(rid).last_guide_reach, long_reach)

    def test_a_rule_that_cannot_run_keeps_the_reach_from_its_last_run(self):
        """§2: last_guide_reach is the reach at the last evaluation that ran the rule; a check
        that finds it broken moves last_evaluated_at and leaves the reach alone."""
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        first = self.get_rule(rid)
        ran_at = first.last_evaluated_at
        self.assertEqual(first.last_guide_reach, self.reach_b)
        first.search_params = first.search_params + '&f.nosuchdimension=1'
        db.session.commit()
        self.evaluate(rid)
        rule = self.get_rule(rid)
        self.assertTrue(rule.last_error)
        self.assertGreater(rule.last_evaluated_at, ran_at)
        self.assertEqual(rule.last_guide_reach, self.reach_b)

    def test_a_source_its_matches_draw_from_that_is_empty_leaves_the_reach_null(self):
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        self.assertEqual(self.get_rule(rid).last_guide_reach, self.reach_b)
        EPGEntry.query.filter_by(source_id=self.src_b.id).delete()
        db.session.commit()
        self.evaluate(rid)
        rule = self.get_rule(rid)
        self.assertIsNotNone(rule.last_evaluated_at)
        self.assertIsNone(rule.last_guide_reach)
        alerts = self.guide_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Drained Source', alerts[0].body)


class GuideShortAlertTests(_SourcesCase):

    def test_an_empty_guide_with_an_enabled_rule_raises_it(self):
        EPGEntry.query.delete()
        db.session.commit()
        rid = self.rule('grain=airings&q=Pick')
        self.evaluate(rid)
        self.assertEqual(len(self.guide_alerts()), 1)
        self.assertIsNone(self.get_rule(rid).last_guide_reach)

    def test_no_enabled_rule_raises_nothing_however_short_the_guide(self):
        EPGEntry.query.delete()
        db.session.commit()
        rid = self.rule('grain=airings&q=Pick')
        auto_rules.set_rule_enabled(rid, False)
        self.run_all()
        self.assertEqual(self.guide_alerts(), [])

    def test_one_drained_source_raises_it_while_another_is_fresh(self):
        """The guide-wide reach is fine; the source this rule's match draws from is not."""
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        alerts = self.guide_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Drained Source', alerts[0].body)
        self.assertNotIn('Fresh Source', alerts[0].body)
        self.assertNotIn('The guide only reaches', alerts[0].body)

        self.showing(self.ch_b, 'Filler Bravo Later', _future(hours=36))
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.guide_alerts(), [], 'clears once every relevant reach is a day')

    def test_a_group_match_is_judged_by_its_longest_reaching_member(self):
        """§4.5: a group row has listings while any member does, so a group match whose best
        member reaches far does not raise the alert for a member's short source - the alert
        and the rule's own reach answer the one question the same way."""
        acc_c = seed.make_account(name='Third Provider')
        src_c = seed.make_epg_source(acc_c, name='Group Short Source')
        acc_d = seed.make_account(name='Fourth Provider')
        src_d = seed.make_epg_source(acc_d, name='Group Long Source')
        m1 = self.channel('Member One', account=acc_c, epg_source_id=src_c.id)
        m2 = self.channel('Member Two', account=acc_d, epg_source_id=src_d.id)
        seed.make_group(name='Reach Group', members=[m1, m2])
        self.showing(m1, 'Grouped Pick', _future(hours=2))
        self.showing(m1, 'Member One Filler', _future(hours=5))
        self.showing(m2, 'Member Two Filler', _future(hours=40))
        db.session.commit()
        rid = self.rule('grain=airings&q=Grouped+Pick')
        self.evaluate(rid)
        self.assertEqual(self.guide_alerts(), [])

    def test_a_drained_source_no_rule_depends_on_raises_nothing(self):
        rid = self.rule('grain=airings&q=Pick+Alpha')
        self.evaluate(rid)
        self.assertEqual(self.guide_alerts(), [])

    def test_a_short_guide_raises_it_and_a_long_one_clears_it(self):
        EPGEntry.query.delete()
        db.session.commit()
        self.showing(self.ch_a, 'Pick Alpha', _future(hours=2))
        db.session.commit()
        rid = self.rule('grain=airings&q=Pick+Alpha')
        self.evaluate(rid)
        alerts = self.guide_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Fresh Source', alerts[0].body)
        self.showing(self.ch_a, 'Filler Alpha', _future(hours=48))
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.guide_alerts(), [])

    def test_deleting_the_last_enabled_rule_clears_it_and_deleting_one_of_two_does_not(self):
        first = self.rule('grain=airings&q=Pick+Bravo', name='first')
        second = self.rule('grain=airings&q=Pick+Bravo', name='second')
        self.run_all()
        self.assertEqual(len(self.guide_alerts()), 1)
        auto_rules.delete_rule(first, cancel_scheduled=False)
        self.assertEqual(len(self.guide_alerts()), 1, 'an enabled rule still depends on it')
        auto_rules.delete_rule(second, cancel_scheduled=False)
        self.assertEqual(self.guide_alerts(), [])

    def test_pausing_the_last_enabled_rule_clears_it(self):
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        self.assertEqual(len(self.guide_alerts()), 1)
        auto_rules.set_rule_enabled(rid, False)
        self.assertEqual(self.guide_alerts(), [])

    def test_an_evaluation_that_finds_no_enabled_rule_still_recomputes_it(self):
        """§6: every evaluation recomputes it - a rule switched off by any path leaves no
        alert behind once the next run finds nothing enabled."""
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        self.assertEqual(len(self.guide_alerts()), 1)
        db.session.get(AutoRule, rid).enabled = False
        db.session.commit()
        self.assertEqual(self.run_all(), {})
        self.assertEqual(self.guide_alerts(), [])


# ---------------------------------------------------------------------------
# §6 - AUTO_RULE_CAPPED and AUTO_RULE_BROKEN
# ---------------------------------------------------------------------------

class AlertRegistryTests(unittest.TestCase):

    def test_the_three_types_are_registered_warn_and_self_clearing_and_not_retired(self):
        for alert_type in (AUTO_RULE_CAPPED, AUTO_RULE_BROKEN, AUTO_RULES_GUIDE_SHORT):
            with self.subTest(alert_type=alert_type):
                self.assertIn(alert_type, ALERT_TYPES)
                self.assertEqual(ALERT_TYPES[alert_type]['severity'], 'WARN')
                self.assertTrue(is_self_clearing(alert_type))
                self.assertNotIn(alert_type, RETIRED_ALERT_TYPES)


class CappedAlertTests(_EngineCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.showing(ch, 'Cup Race Two', _future(hours=10), minutes=180)
        self.far_filler()
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race', max_upcoming=1)
        out = self.evaluate(self.rid)
        self.assertEqual((out['scheduled'], out['skipped']), (1, {RULE_MATCH_SKIPPED_CAP: 1}))
        self.assertEqual(len(self.cap_alerts(self.rid)), 1)

    def test_it_clears_once_the_held_back_showings_have_started(self):
        """§6: "an evaluation leaves none" - including because what was held back started.
        The clock moves past Cup Race Two's start while it is still on the air."""
        real = datetime

        class Later(real):
            @classmethod
            def utcnow(cls):
                return real.utcnow() + timedelta(hours=10, minutes=30)

        with patch('app.channel_search.datetime', Later), \
                patch('app.auto_rules.datetime', Later):
            out = self.evaluate(self.rid)
        self.assertEqual(out['skipped'], {})
        self.assertEqual(self.cap_alerts(self.rid), [])

    def test_it_clears_once_an_evaluation_leaves_nothing_at_the_cap(self):
        rec = self.recordings(self.rid)[0]
        rec.status = REC_STATUS_COMPLETED
        db.session.commit()
        out = self.evaluate(self.rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(self.cap_alerts(self.rid), [])
        self.assertEqual({m.action_taken for m in self.ledger(self.rid)}, {RULE_MATCH_SCHEDULED})

    def test_a_second_capped_evaluation_keeps_one_alert(self):
        self.evaluate(self.rid)
        self.assertEqual(len(self.cap_alerts(self.rid)), 1)

    def test_pause_clears_it(self):
        auto_rules.set_rule_enabled(self.rid, False)
        self.assertEqual(self.cap_alerts(self.rid), [])

    def test_edit_clears_it(self):
        rule = db.session.get(AutoRule, self.rid)
        auto_rules.update_rule(self.rid, auto_rules.read_rule_values({'name': 'renamed'},
                                                                     existing=rule))
        self.assertEqual(self.cap_alerts(self.rid), [])

    def test_delete_clears_it(self):
        auto_rules.delete_rule(self.rid, cancel_scheduled=True)
        self.assertEqual(self.cap_alerts(self.rid), [])


class BrokenAlertTests(_EngineCase):

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.far_filler()
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race&x.tag=nosuchtag')
        self.evaluate(self.rid)
        self.assertEqual(len(self.broken_alerts(self.rid)), 1)
        self.assertIn('nosuchtag', self.get_rule(self.rid).last_error or '')

    def test_pause_clears_it(self):
        auto_rules.set_rule_enabled(self.rid, False)
        self.assertEqual(self.broken_alerts(self.rid), [])

    def test_edit_clears_it(self):
        rule = db.session.get(AutoRule, self.rid)
        auto_rules.update_rule(self.rid, auto_rules.read_rule_values(
            {'search': 'grain=airings&q=Cup+Race'}, existing=rule))
        self.assertEqual(self.broken_alerts(self.rid), [])

    def test_delete_clears_it(self):
        auto_rules.delete_rule(self.rid, cancel_scheduled=False)
        self.assertEqual(self.broken_alerts(self.rid), [])

    def test_an_evaluation_that_runs_it_clears_it(self):
        db.session.get(AutoRule, self.rid).search_params = 'q=Cup+Race&grain=airings'
        db.session.commit()
        out = self.evaluate(self.rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertEqual(self.broken_alerts(self.rid), [])
        self.assertIsNone(self.get_rule(self.rid).last_error)


class LastErrorSplitTests(_EngineCase):
    """DESIGN-autoschedule.md §2, dev/changelog/1189: last_error means the rule could not run
    and last_problems means a check that ran could not place a showing. Each write sets its
    own column and clears the other, so the Rules page never calls a running rule "not
    running", or a broken one healthy because an old problem sentence was cleared."""

    def setUp(self):
        super().setUp()
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.far_filler()
        db.session.commit()
        self.rid = self.rule('grain=airings&q=Cup+Race')

    def _place_nothing(self):
        with patch('app.auto_rules.add_recording_event',
                   side_effect=RuntimeError('event table unavailable')):
            self.evaluate(self.rid)

    def test_a_check_that_ran_with_a_problem_is_not_an_error(self):
        self._place_nothing()
        rule = self.get_rule(self.rid)
        self.assertIn('Cup Race One', rule.last_problems or '')
        self.assertIsNone(rule.last_error)

    def _stale_problem(self):
        db.session.get(AutoRule, self.rid).last_problems = 'An earlier check left this.'
        db.session.commit()

    def test_breaking_after_a_problem_clears_the_problem(self):
        self._stale_problem()
        db.session.get(AutoRule, self.rid).search_params = (
            'grain=airings&q=Cup+Race&x.tag=nosuchtag')
        db.session.commit()
        self.evaluate(self.rid)
        rule = self.get_rule(self.rid)
        self.assertIn('nosuchtag', rule.last_error or '')
        self.assertIsNone(rule.last_problems)

    def test_running_again_after_breaking_clears_the_error(self):
        db.session.get(AutoRule, self.rid).search_params = (
            'grain=airings&q=Cup+Race&x.tag=nosuchtag')
        db.session.commit()
        self.evaluate(self.rid)
        db.session.get(AutoRule, self.rid).search_params = 'grain=airings&q=Cup+Race'
        db.session.commit()
        self._place_nothing()
        rule = self.get_rule(self.rid)
        self.assertIsNone(rule.last_error)
        self.assertIn('Cup Race One', rule.last_problems or '')

    def test_a_check_that_raised_clears_the_problem(self):
        self._stale_problem()
        with patch('app.auto_rules._search_rule', side_effect=RuntimeError('search exploded')):
            self.evaluate(self.rid)
        rule = self.get_rule(self.rid)
        self.assertIn('search exploded', rule.last_error or '')
        self.assertIsNone(rule.last_problems)


# ---------------------------------------------------------------------------
# §7 - the interleavings that do not need retrack
# ---------------------------------------------------------------------------

class InterleavingTests(_EngineCase):

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.far_filler()
        db.session.commit()

    def test_r1_a_padding_overlap_on_the_same_channel_is_a_handoff_not_a_skip(self):
        start = _future(hours=4)
        self.showing(self.ch, 'Cup Race One', start, minutes=120)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='the show before',
                            channel_id=self.ch.id, start_time=start - timedelta(hours=2),
                            stop_time=start + timedelta(minutes=15))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)

    def test_r1_a_same_channel_recording_covering_the_whole_air_time_skips_it(self):
        start = _future(hours=4)
        self.showing(self.ch, 'Cup Race One', start, minutes=120)
        mine = seed.make_recording(status=REC_STATUS_SCHEDULED, name='block by hand',
                                   channel_id=self.ch.id, start_time=start - timedelta(hours=1),
                                   stop_time=start + timedelta(hours=3))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        row = self.ledger(rid)[0]
        self.assertEqual(row.action_taken, RULE_MATCH_SKIPPED_EXISTING)
        self.assertIn(str(mine.id), row.detail or '')

    def test_r2_r6_the_first_rule_wins_and_a_cancelled_winner_hands_over(self):
        """R2: two rules on one program - the first recording made wins, the other is
        skipped_existing and takes over when the winner is cancelled. R6: the cancelled
        winner's rule never recreates it; its row stays `scheduled` with the fate stamped."""
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        db.session.commit()
        first = self.rule('grain=airings&q=Cup+Race', name='first')
        second = self.rule('grain=airings&q=Cup+Race+One', name='second')
        self.run_all()
        winner = self.recordings(first)
        self.assertEqual(len(winner), 1)
        self.assertEqual(self.recordings(second), [])
        self.assertEqual([m.action_taken for m in self.ledger(second)],
                         [RULE_MATCH_SKIPPED_EXISTING])

        resp = self.t.client.post(f'/recordings/{winner[0].id}/cancel-json')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        row = self.ledger(first)[0]
        self.assertEqual((row.action_taken, row.recording_id, row.removed_status),
                         (RULE_MATCH_SCHEDULED, None, REC_STATUS_SCHEDULED))

        self.run_all()
        self.assertEqual(self.recordings(first), [], 'R6: a cancelled pick is never recreated')
        self.assertEqual(len(self.recordings(second)), 1, 'R2: the other rule takes over')
        second_rows = self.ledger(second)
        self.assertEqual([m.action_taken for m in second_rows], [RULE_MATCH_SCHEDULED])

    def test_r2_an_aborted_winner_hands_over_too(self):
        self.showing(self.ch, 'Cup Race One', _future(hours=4))
        db.session.commit()
        first = self.rule('grain=airings&q=Cup+Race', name='first')
        second = self.rule('grain=airings&q=Cup+Race+One', name='second')
        self.run_all()
        rec = self.recordings(first)[0]
        rec.status = REC_STATUS_ABORTED
        rec.cancel_reason = CANCEL_BEFORE_START
        db.session.commit()
        self.run_all()
        self.assertEqual(len(self.recordings(second)), 1)
        self.assertEqual([r.status for r in self.recordings(first)], [REC_STATUS_ABORTED])

    def test_r7_an_account_with_no_free_connection_is_scheduled_with_the_warning(self):
        acc = seed.make_account(name='One Connection', max_connections=1)
        busy = self.channel('Busy', account=acc)
        target = self.channel('Target', account=acc)
        start = _future(hours=4)
        seed.make_recording(status=REC_STATUS_SCHEDULED, name='already booked',
                            channel_id=busy.id, start_time=start - timedelta(minutes=10),
                            stop_time=start + timedelta(hours=2))
        self.showing(target, 'Cup Race One', start)
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        rec = self.recordings(rid)[0]
        self.assertEqual(rec.channel_id, target.id)
        row = self.ledger(rid)[0]
        self.assertIn('Scheduled with a warning', row.detail or '')
        ev = RecordingEvent.query.filter_by(recording_id=rec.id,
                                            event_type=RECORDING_RULE_SCHEDULED).one()
        self.assertIn('connection_limit_warning', json.loads(ev.extra_data)['warnings'])

    def test_r8_a_failed_original_makes_its_replay_no_longer_a_repeat(self):
        self.showing(self.ch, 'Cup Race', _future(hours=4), sub_title='Daytona 500')
        self.showing(self.ch, 'Cup Race', _future(hours=50), sub_title='Daytona 500')
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        self.evaluate(rid)
        rows = self.ledger(rid)
        self.assertEqual([m.action_taken for m in rows],
                         [RULE_MATCH_SCHEDULED, RULE_MATCH_SKIPPED_REPEAT])
        replay_row_id = rows[1].id
        original = self.recordings(rid)[0]
        original.status = REC_STATUS_FAILED
        db.session.commit()
        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        rows = self.ledger(rid)
        self.assertEqual([m.action_taken for m in rows],
                         [RULE_MATCH_SCHEDULED, RULE_MATCH_SCHEDULED])
        self.assertEqual(rows[1].id, replay_row_id, 'the re-judged row is updated in place')


# ---------------------------------------------------------------------------
# §11 - scaling
# ---------------------------------------------------------------------------

class ScalingTests(unittest.TestCase):
    """Each measurement builds its own app, so no measurement inherits another's rows."""

    def _app(self):
        t = make_test_app()
        patcher = patch('app.routes.recordings.schedule_recording')
        patcher.start()
        acc = seed.make_account()
        channels = [seed.make_channel(acc, stream_id=200 + i, name=f'Scale {i}')
                    for i in range(5)]
        seed.make_epg_entry(seed.make_channel(acc, stream_id=300, name='Far'), title='Zzfiller',
                            start_time=_future(hours=500))
        db.session.commit()
        return t, patcher, channels

    def _close(self, t, patcher):
        try:
            admission.reset_for_tests()
            t.cleanup()
        finally:
            patcher.stop()

    def _seed_showings(self, channels, title, n, spacing_hours=1):
        base = _future(hours=2)
        for i in range(n):
            seed.make_epg_entry(channels[i % len(channels)], title=title,
                                start_time=base + timedelta(hours=spacing_hours * i),
                                duration_minutes=30)
        db.session.commit()

    def _measure_re_evaluation(self, n):
        t, patcher, channels = self._app()
        try:
            self._seed_showings(channels, 'Alphascale Hour', n)
            rid = auto_rules.create_rule(auto_rules.read_rule_values(
                {'name': 'scale', 'search': 'grain=airings&q=Alphascale', 'max_upcoming': 3}))
            first = auto_rules.run_evaluation(auto_rules.TRIGGER_SAVE, [rid])[rid]
            self.assertEqual(first.scheduled, 3)
            self.assertEqual(first.skipped, {RULE_MATCH_SKIPPED_CAP: n - 3})
            db.session.expire_all()
            config_mod._yaml_cache = None
            loads = []

            def counting(*args, **kwargs):
                loads.append(1)
                return _REAL_LOAD_CONFIG(*args, **kwargs)

            with patch('app.config.load_config', side_effect=counting), \
                    IOCounter(all_engines()) as counter:
                again = auto_rules.run_evaluation(auto_rules.TRIGGER_SAVE, [rid])[rid]
            self.assertEqual(again.scheduled, 0)
            writes = [s for s in counter.statements
                      if s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                      and ('rule_matches' in s or 'recordings' in s)]
            self.assertEqual(writes, [], 'an unchanged verdict writes nothing')
            return counter.queries, counter.config_parses, len(loads)
        finally:
            self._close(t, patcher)

    def test_re_evaluating_decided_matches_costs_the_same_for_5_as_for_400(self):
        small = self._measure_re_evaluation(5)
        large = self._measure_re_evaluation(400)
        self.assertEqual(small[0], large[0],
                         f'SQL statements scale with matches: {small[0]} at 5, {large[0]} at 400')
        self.assertEqual(small[1], large[1],
                         f'config parses scale with matches: {small[1]} at 5, {large[1]} at 400')
        self.assertEqual(small[2], large[2],
                         f'load_config() calls scale with matches: {small[2]} at 5, '
                         f'{large[2]} at 400')

    def _first_evaluation_commits(self, n):
        t, patcher, channels = self._app()
        try:
            self._seed_showings(channels, 'Betascale Hour', n)
            rid = auto_rules.create_rule(auto_rules.read_rule_values(
                {'name': 'broad', 'search': 'grain=airings&q=Betascale', 'max_upcoming': 2}))
            with _CommitLog() as log:
                out = auto_rules.run_evaluation(auto_rules.TRIGGER_SAVE, [rid])[rid]
            self.assertEqual(out.scheduled, 2)
            skip_commits = [c for c in log.commits
                            if any(kind == 'RuleMatch' and detail in RULE_MATCH_SKIPS
                                   for kind, detail in c)]
            self.assertEqual(len(skip_commits), 1, 'every skip in one commit')
            self.assertEqual(sum(1 for kind, detail in skip_commits[0]
                                 if kind == 'RuleMatch' and detail in RULE_MATCH_SKIPS), n - 2)
            return len(log.commits)
        finally:
            self._close(t, patcher)

    def test_a_broad_first_evaluation_writes_its_skips_in_one_commit(self):
        small = self._first_evaluation_commits(12)
        large = self._first_evaluation_commits(80)
        self.assertEqual(small, large,
                         f'commits scale with skipped showings: {small} at 12, {large} at 80')

    def _scheduling_cost(self, k):
        t, patcher, channels = self._app()
        try:
            self._seed_showings(channels[:1], 'Gammascale Hour', k, spacing_hours=6)
            rid = auto_rules.create_rule(auto_rules.read_rule_values(
                {'name': 'k', 'search': 'grain=airings&q=Gammascale', 'max_upcoming': 0}))
            db.session.expire_all()
            config_mod._yaml_cache = None
            with IOCounter(all_engines()) as counter:
                out = auto_rules.run_evaluation(auto_rules.TRIGGER_SAVE, [rid])[rid]
            self.assertEqual(out.scheduled, k)
            return counter.queries, counter.config_parses
        finally:
            self._close(t, patcher)

    def test_scheduling_k_showings_costs_a_fixed_amount_per_showing(self):
        costs = {k: self._scheduling_cost(k) for k in (2, 4, 6)}
        step_one = costs[4][0] - costs[2][0]
        step_two = costs[6][0] - costs[4][0]
        self.assertGreater(step_one, 0)
        self.assertEqual(step_one, step_two,
                         f'per-showing cost is not fixed: statements {costs}')
        self.assertEqual(len({parses for _q, parses in costs.values()}), 1,
                         f'config parses grow with showings: {costs}')


# ---------------------------------------------------------------------------
# The rules API's JSON envelope
# ---------------------------------------------------------------------------

class EnvelopeTests(_EngineCase):

    def test_a_bad_search_is_400_with_an_error(self):
        resp = self.t.client.post('/api/auto-rules', json={
            'name': 'r', 'search': 'grain=airings&q=x&standing=grpdedup&standing=showpast'})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(isinstance(body.get('error'), str) and body['error'])
        self.assertNotIn('success', body)
        self.assertNotIn('ok', set(body))
        self.assertEqual(AutoRule.query.count(), 0)

    def test_a_save_answers_success_the_rule_and_the_evaluation(self):
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race One', _future(hours=4))
        self.far_filler()
        db.session.commit()
        resp = self.t.client.post('/api/auto-rules', json={
            'name': 'cup', 'search': 'grain=airings&q=Cup+Race'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertIs(body.get('success'), True)
        self.assertNotIn('ok', set(body))
        self.assertEqual(body['rule']['name'], 'cup')
        self.assertIsInstance(body['rule']['id'], int)
        evaluation = body['evaluation']
        self.assertIs(evaluation['queued'], False)
        self.assertEqual(evaluation['scheduled'], 1)
        self.assertEqual(evaluation['skipped'], {})
        self.assertIn('guide_reach', evaluation)

    def test_a_first_check_that_raises_still_answers_that_the_rule_is_saved(self):
        """The rule is committed before its first check runs, so a check that raises must
        not answer 500 - the dialog would read it as "not saved" and a second click would
        make a second, identical rule (dev/changelog/1186)."""
        with patch('app.auto_rules.evaluate_now', side_effect=RuntimeError('boom')), \
                patch('app.auto_rules.queue_evaluation') as queued:
            resp = self.t.client.post('/api/auto-rules', json={
                'name': 'cup', 'search': 'grain=airings&q=Cup+Race'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertIs(body['success'], True)
        self.assertIn('boom', body['evaluation']['error'])
        self.assertEqual(AutoRule.query.count(), 1)
        queued.assert_called_once()

    def test_an_unknown_rule_is_404_with_an_error(self):
        for method, url, body in (('put', '/api/auto-rules/999', {'name': 'x'}),
                                  ('post', '/api/auto-rules/999/pause', None),
                                  ('post', '/api/auto-rules/999/resume', None),
                                  ('delete', '/api/auto-rules/999', {'cancel_scheduled': False})):
            with self.subTest(method=method, url=url):
                resp = getattr(self.t.client, method)(url, json=body)
                self.assertEqual(resp.status_code, 404)
                self.assertTrue(resp.get_json().get('error'))

    def test_a_delete_must_say_whether_to_cancel(self):
        rid = self.rule('grain=airings&q=Cup+Race')
        resp = self.t.client.delete(f'/api/auto-rules/{rid}', json={})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json().get('error'))
        self.assertIsNotNone(self.get_rule(rid))


# ---------------------------------------------------------------------------
# Holes the review of dev/changelog/1186 found before it shipped
# ---------------------------------------------------------------------------

class ClaimTests(_EngineCase):
    """§4.3 "every write re-checks the rule", made atomic. A re-check that is only a SELECT
    runs in autocommit under pysqlite, outside the write's transaction, so an edit committing
    between the check and the commit was never seen. The claim is a conditional write: it
    refuses a moved rule, and while it holds nothing else can commit (dev/changelog/1186)."""

    def test_the_claim_refuses_a_moved_paused_or_deleted_rule(self):
        rid = self.rule('grain=airings&q=Cup')
        rev = self.get_rule(rid).revision
        self.assertTrue(auto_rules._claim_rule(rid, rev))
        db.session.rollback()
        self.assertFalse(auto_rules._claim_rule(rid, rev + 1), 'another revision')
        db.session.rollback()
        auto_rules.set_rule_enabled(rid, False)
        paused = self.get_rule(rid)
        self.assertFalse(auto_rules._claim_rule(rid, paused.revision), 'paused')
        db.session.rollback()
        auto_rules.delete_rule(rid, cancel_scheduled=False)
        self.assertFalse(auto_rules._claim_rule(rid, paused.revision), 'deleted')
        db.session.rollback()

    def test_an_edit_cannot_commit_while_a_claim_holds(self):
        import sqlite3
        rid = self.rule('grain=airings&q=Cup')
        rev = self.get_rule(rid).revision
        self.assertTrue(auto_rules._claim_rule(rid, rev))
        other = sqlite3.connect(self.t.db_path, timeout=0.2)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                other.execute('UPDATE auto_rules SET revision = revision + 1 WHERE id = ?',
                              (rid,))
            db.session.rollback()
            other.execute('UPDATE auto_rules SET revision = revision + 1 WHERE id = ?', (rid,))
            other.commit()
        finally:
            other.close()
        self.assertEqual(self.get_rule(rid).revision, rev + 1)


class ReimportedListingTests(_EngineCase):
    """An import that runs between the search and the plan (a sync never yields to a rule
    check) deletes and re-inserts listings, and SQLite can hand a deleted listing's id to a
    different program. The planner reads by id, so without a check the rule would record
    whatever now holds it while its ledger row said the judged race was scheduled - and never
    look at the race again (dev/changelog/1186)."""

    def test_a_listing_replaced_under_its_id_is_not_recorded_as_the_judged_program(self):
        from app.routes import recordings as rec_routes
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=5), sub_title='Fall 400')
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        real = rec_routes._plan_bulk_schedule

        def replaced(items, *args, **kwargs):
            entry = db.session.get(EPGEntry, items[0][0])
            entry.title, entry.sub_title = 'Bass Fishing Today', None
            db.session.commit()
            return real(items, *args, **kwargs)

        with patch('app.routes.recordings._plan_bulk_schedule', side_effect=replaced):
            out = self.evaluate(rid)
        self.assertEqual(self.recordings(), [])
        self.assertEqual(self.ledger(rid), [], 'not judged on its merits, so no row')
        self.assertEqual(out['scheduled'], 0)
        self.assertIn('the guide changed', out['problems'])
        self.assertIn('the guide changed', self.get_rule(rid).last_problems)


class OutcomePayloadTests(_EngineCase):
    """What a save, edit or resume is told. A check that did not run (`error`) and a check
    that ran but could not place one showing (`problems`) mean opposite things, and a re-check
    of matches that are already decided is not "nothing matches" (dev/changelog/1186)."""

    def test_a_showing_the_check_could_not_place_is_a_problem_not_a_stopped_check(self):
        from app.routes import recordings as rec_routes
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=3), sub_title='One')
        self.showing(ch, 'Cup Race', _future(hours=30), sub_title='Two')
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        real = rec_routes._plan_bulk_schedule
        calls = []

        def second_is_gone(items, *args, **kwargs):
            calls.append(items)
            if len(calls) == 2:
                return [{'epg_id': items[0][0], 'group_id': None, 'verdict': rec_routes.BULK_SKIP,
                         'reason': 'This showing is no longer in the guide data.',
                         'warnings': {}}]
            return real(items, *args, **kwargs)

        with patch('app.routes.recordings._plan_bulk_schedule', side_effect=second_is_gone):
            out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 1)
        self.assertIsNone(out['error'])
        self.assertIn('no longer in the guide data', out['problems'])

    def test_a_recheck_counts_what_earlier_checks_already_scheduled(self):
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=3), sub_title='One')
        self.showing(ch, 'Cup Race', _future(hours=30), sub_title='Two')
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        first = self.evaluate(rid)
        self.assertEqual((first['scheduled'], first['already_scheduled']), (2, 0))
        again = self.evaluate(rid)
        self.assertEqual((again['scheduled'], again['already_scheduled']), (0, 2))

    def test_a_pick_the_user_cancelled_is_not_counted_as_already_scheduled(self):
        """R6: the cancelled pick stays decided - never recreated - but nothing will record
        it, so "already scheduled" must not count it."""
        from app.routes.recordings import _unschedule_and_delete_row
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=3), sub_title='One')
        self.showing(ch, 'Cup Race', _future(hours=30), sub_title='Two')
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        self.evaluate(rid)
        with patch('app.routes.recordings.unschedule_recording'):
            _unschedule_and_delete_row(self.recordings(rule_id=rid)[0].id)
        again = self.evaluate(rid)
        self.assertEqual((again['scheduled'], again['already_scheduled']), (0, 1))


class LongAgoMatchReachTests(_SourcesCase):
    """§4.5: a rule's reach comes from the sources its ledger matches draw from - all of them
    the ledger still holds. A recent-only window let a drained source age out of the alert
    while it was still drained, a week or so after a weekly rule last matched on it
    (dev/changelog/1186)."""

    def test_a_source_matched_weeks_ago_still_counts_while_it_is_drained(self):
        rid = self.rule('grain=airings&q=Pick+Bravo')
        self.evaluate(rid)
        self.assertEqual(len(self.guide_alerts()), 1)
        for m in self.ledger(rid):
            m.program_start_time -= timedelta(days=21)
            m.program_stop_time -= timedelta(days=21)
        EPGEntry.query.filter_by(title='Pick Bravo').delete()
        db.session.commit()
        self.evaluate(rid)
        self.assertEqual(self.get_rule(rid).last_guide_reach, self.reach_b)
        alerts = self.guide_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Drained Source', alerts[0].body)


class DeleteRaceTests(_EngineCase):
    """§8: a delete is one unit. It takes the write lock before it lists the rule's
    recordings, so a create that is mid-commit when the delete arrives is either listed (and
    cancelled or unlinked) or fails its claim - never left SCHEDULED pointing at a rule that
    no longer exists (dev/changelog/1186)."""

    def test_a_recording_committed_while_the_delete_waits_is_cancelled_with_the_rest(self):
        import sqlite3
        import threading
        rid = self.rule('grain=airings&q=Cup')
        raw = sqlite3.connect(self.t.db_path, timeout=5, isolation_level=None)
        raw.execute('BEGIN IMMEDIATE')
        start = _future(hours=5)
        raw.execute(
            "INSERT INTO recordings (name, url, start_time, stop_time, status, rule_id, "
            "dead_stream_retry_count, metadata_locked) VALUES (?, ?, ?, ?, 'SCHEDULED', ?, 0, 0)",
            ('In-flight pick', 'http://example.test/x', start, start + timedelta(hours=1), rid))
        result = {}

        def delete():
            with self.t.app.app_context():
                result['out'] = auto_rules.delete_rule(rid, cancel_scheduled=True)

        worker = threading.Thread(target=delete)
        worker.start()
        worker.join(0.5)
        self.assertTrue(worker.is_alive(), 'the delete must wait for the write lock')
        raw.execute('COMMIT')
        worker.join(10)
        raw.close()
        self.assertEqual(result['out'][0] and len(result['out'][0]), 1)
        db.session.expire_all()
        self.assertEqual(Recording.query.filter_by(rule_id=rid).count(), 0)
        self.assertEqual(Recording.query.filter_by(name='In-flight pick').count(), 0)


class MidRunChangeTests(_EngineCase):
    """Things the user does while a run is between two rules. The run reads its context
    once; a rename, a tag delete or a profile delete committed after that must give each
    later rule the outcome the spec names for it - not a false "broken", not a crash
    (dev/changelog/1186)."""

    def setUp(self):
        super().setUp()
        self.ch = self.channel('Speed')
        self.far_filler()
        db.session.commit()

    def _during_first_rule(self, change):
        """Run every rule, landing `change` while the first rule is being searched."""
        real = auto_rules._search_rule
        done = []

        def search(state, run):
            if not done:
                done.append(1)
                change()
            return real(state, run)

        with patch('app.auto_rules._search_rule', side_effect=search):
            return self.run_all(auto_rules.TRIGGER_SWEEP)

    def test_a_group_renamed_mid_run_does_not_break_the_rule_that_names_it(self):
        member = self.channel('Member')
        group = seed.make_group(name='Racing', members=[member])
        self.showing(member, 'Group Race', _future(hours=6))
        db.session.commit()
        first = self.rule('grain=airings&q=Nothing+At+All', name='a first')
        second = self.rule('grain=airings&q=Group+Race&f.group=Racing', name='b second')

        def rename():
            self.t.client.post(f'/api/channel-groups/{group.id}/rename',
                               json={'name': 'Motorsport'})

        out = self._during_first_rule(rename)
        self.assertFalse(out[second].stopped, out[second])
        self.assertEqual(self.broken_alerts(second), [])
        self.assertEqual(len(self.recordings(rule_id=second)), 1)
        self.assertIsNotNone(first)

    def test_a_tag_deleted_mid_run_never_crashes_the_rule_and_the_next_run_breaks_it(self):
        """The run judges with the tag as it read it - what the user had a moment before -
        and the next evaluation names the deleted tag the ordinary way (§3.2)."""
        from app.database import Tag, TagPattern
        tag = Tag(name='racing', color='#fff', patterns=[TagPattern(pattern='Race')])
        db.session.add(tag)
        self.showing(self.ch, 'Tagged Race', _future(hours=6))
        db.session.commit()
        self.rule('grain=airings&q=Nothing+At+All', name='a first')
        second = self.rule('grain=airings&f.tag=racing', name='b second')

        def delete_tag():
            self.t.client.delete(f'/api/tags/{tag.id}')

        out = self._during_first_rule(delete_tag)
        self.assertNotIn('stopped on an error', out[second].stopped)
        again = self.run_all(auto_rules.TRIGGER_SWEEP)
        self.assertIn('the tag "racing"', again[second].stopped)
        self.assertEqual(len(self.broken_alerts(second)), 1)

    def test_a_profile_deleted_mid_run_falls_back_to_each_showings_default(self):
        from app.database import RecordingProfile
        profile = RecordingProfile(name='Padded', pre_padding_minutes=5, post_padding_minutes=30)
        db.session.add(profile)
        self.showing(self.ch, 'Padded Race', _future(hours=6))
        db.session.commit()
        self.rule('grain=airings&q=Nothing+At+All', name='a first')
        second = self.rule('grain=airings&q=Padded+Race', name='b second', profile_id=profile.id)

        def delete_profile():
            self.t.client.delete(f'/api/profiles/{profile.id}')

        out = self._during_first_rule(delete_profile)
        self.assertFalse(out[second].stopped, out[second])
        recs = self.recordings(rule_id=second)
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0].profile_id)

    def test_a_profile_deleted_while_its_rule_plans_does_not_crash_the_rule(self):
        """The profile is read at the moment of planning, never kept from the start of the
        run: an earlier rule's commit expires a run-wide copy, and touching it after the
        delete raised instead of planning."""
        from app.database import RecordingProfile
        from app.routes import recordings as rec_routes
        profile = RecordingProfile(name='Padded', pre_padding_minutes=5, post_padding_minutes=30)
        db.session.add(profile)
        self.showing(self.ch, 'Early Race', _future(hours=3))
        self.showing(self.ch, 'Padded Race', _future(hours=6))
        db.session.commit()
        self.rule('grain=airings&q=Early+Race', name='a first')
        second = self.rule('grain=airings&q=Padded+Race', name='b second', profile_id=profile.id)
        real = rec_routes._plan_bulk_schedule
        calls = []

        profile_id = profile.id

        def plan(items, *args, **kwargs):
            calls.append(items)
            if len(calls) == 2:
                # From another connection, as a request on another thread would: the test
                # client shares this test's session, where a delete detaches the run's copy
                # instead of leaving it expired over a row that is gone.
                import sqlite3
                other = sqlite3.connect(self.t.db_path, timeout=5)
                other.execute('UPDATE auto_rules SET profile_id = NULL WHERE profile_id = ?',
                              (profile_id,))
                other.execute('DELETE FROM recording_profiles WHERE id = ?', (profile_id,))
                other.commit()
                other.close()
            return real(items, *args, **kwargs)

        with patch('app.routes.recordings._plan_bulk_schedule', side_effect=plan):
            out = self.run_all(auto_rules.TRIGGER_SWEEP)
        self.assertEqual(len(calls), 2)
        self.assertFalse(out[second].stopped, out[second])

    def test_a_rule_paused_while_its_check_raises_is_not_stamped_with_the_failure(self):
        rid = self.rule('grain=airings&q=Cup')

        def pause_then_raise(state, run):
            auto_rules.set_rule_enabled(rid, False)
            raise RuntimeError('search exploded')

        with patch('app.auto_rules._search_rule', side_effect=pause_then_raise):
            out = self.run_all(auto_rules.TRIGGER_SWEEP)
        self.assertIn('search exploded', out[rid].stopped)
        rule = self.get_rule(rid)
        self.assertIsNone(rule.last_error)
        self.assertIsNone(rule.last_evaluated_at)


class AlertAfterMoveTests(_EngineCase):
    """A rule's alerts are raised after the write that claimed it has committed. A pause or
    delete landing in that gap cleared the rule's alerts already, so the run takes down what
    it raised rather than leave an alert nothing will ever evaluate away (dev/changelog/1186)."""

    def test_a_cap_alert_raised_after_the_rule_moved_is_taken_back_down(self):
        rid = self.rule('grain=airings&q=Cup')
        old_revision = self.get_rule(rid).revision
        auto_rules.set_rule_enabled(rid, False)
        outcome = auto_rules.RuleOutcome(rule_id=rid, capped=2)
        settings = SimpleNamespace(name='Test rule', cap=3)
        auto_rules._rule_alerts_after_run(rid, old_revision, outcome, settings)
        self.assertEqual(self.cap_alerts(rid), [])


class QueuedReasonAndRunNowTests(_EngineCase):
    """What a held-back check tells the person: the queued job's reason names the blocker
    in plain words, with no age frozen into it, and Run now answers a refusal in the request
    instead of saying "Started" for a run that never happens (dev/changelog/1186)."""

    def test_a_refused_queued_run_is_requeued_waiting_for_the_blocker_by_name(self):
        held = admission.try_start(admission.KIND_SYNC, 'Account 3')
        try:
            with patch('app.scheduler.queue_auto_rule_evaluation') as queue:
                self.assertFalse(scheduler_mod._run_auto_rules('queued'))
        finally:
            admission.release(held)
        self.assertEqual(queue.call_args.kwargs['reason'],
                         'waiting for an account sync to finish')

    def test_run_now_of_the_sweep_is_refused_in_the_request_while_a_sync_holds_the_axis(self):
        held = admission.try_start(admission.KIND_SYNC, 'Account 3')
        try:
            refusal = scheduler_mod.run_system_job_now(scheduler_mod.AUTO_RULE_SWEEP_JOB_ID)
        finally:
            admission.release(held)
        self.assertIsInstance(refusal, admission.Refusal)
        self.assertIn('an account sync', refusal.reason)


class DeleteConfirmNamesRulesTests(_EngineCase):
    """Deleting a tag or a group a rule names stops that rule (§3.2), so both delete
    confirms name the rule before the click (dev/changelog/1186)."""

    def test_the_tag_usage_names_a_rule_that_searches_the_tag(self):
        from app.database import Tag
        from app.routes.tags import _tag_usage
        db.session.add(Tag(name='racing', color='#fff'))
        db.session.commit()
        self.rule('grain=airings&x.tag=racing', name='No replays')
        self.assertIn('the "No replays" auto-record rule', _tag_usage().get('racing', []))

    def test_the_groups_page_payload_carries_the_rules_naming_each_group(self):
        member = self.channel('Member')
        seed.make_group(name='Racing', members=[member])
        db.session.commit()
        self.rule('grain=airings&f.group=Racing', name='Cup on Racing')
        html = self.t.client.get('/channel-groups').get_data(as_text=True)
        self.assertIn('Cup on Racing', html)


class FinalReviewFixTests(_EngineCase):
    """What the last review of dev/changelog/1186 found in the fixes themselves."""

    def test_a_queued_check_can_overlap_the_one_still_running(self):
        """A date job skipped for "max instances" is removed, so a check queued while the
        previous queued one runs would silently wait for the hourly sweep."""
        with patch('app.scheduler.scheduler_is_live', return_value=True), \
                patch('app.scheduler._add_job') as add:
            self.assertTrue(scheduler_mod.queue_auto_rule_evaluation(reason='x'))
        self.assertGreaterEqual(add.call_args.kwargs.get('max_instances', 1), 2)

    def test_a_stored_error_never_carries_the_statement_or_a_stream_login(self):
        """A database error's text holds the whole INSERT and its parameters - a stream URL
        with the account's login in its path. last_problems, the toast and the page show it."""
        from sqlalchemy.exc import OperationalError
        exc = OperationalError(
            'INSERT INTO recordings (url) VALUES (?)',
            ('http://provider.test/live/someuser/s3cret/123.ts',),
            Exception('database is locked'))
        text = auto_rules.describe_error(exc)
        self.assertIn('database is locked', text)
        self.assertNotIn('s3cret', text)
        self.assertNotIn('INSERT', text)
        plain = auto_rules.describe_error(
            RuntimeError('fetch http://provider.test/live/someuser/s3cret/9.ts failed'))
        self.assertNotIn('s3cret', plain)

    def test_a_create_that_fails_in_the_database_stores_no_login(self):
        from sqlalchemy.exc import OperationalError
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=4))
        self.far_filler()
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        boom = OperationalError('INSERT INTO recordings (url) VALUES (?)',
                                ('http://provider.test/live/someuser/s3cret/123.ts',),
                                Exception('database is locked'))
        with patch('app.routes.recordings._create_scheduled_recording', side_effect=boom):
            out = self.evaluate(rid)
        self.assertIn('database is locked', out['problems'])
        self.assertNotIn('s3cret', out['problems'])
        self.assertNotIn('s3cret', self.get_rule(rid).last_problems)

    def test_a_pause_during_a_sync_queues_the_guide_check_rather_than_reading_mid_import(self):
        """A sync mid-import has deleted a source's listings and not yet written the new
        ones; reading the reach then raises the alert against a source about to be full."""
        self.rule('grain=airings&q=Cup', name='stays on')
        rid = self.rule('grain=airings&q=Race', name='paused')
        held = admission.try_start(admission.KIND_SYNC, 'Account 3')
        try:
            with patch('app.auto_rules.refresh_guide_alert') as refresh, \
                    patch('app.auto_rules.queue_evaluation') as queued:
                auto_rules.set_rule_enabled(rid, False)
        finally:
            admission.release(held)
        refresh.assert_not_called()
        queued.assert_called_once()
        with patch('app.auto_rules.refresh_guide_alert') as refresh:
            auto_rules.set_rule_enabled(rid, True)
            auto_rules.set_rule_enabled(rid, False)
        refresh.assert_called_once()

    def test_a_rename_holds_the_write_lock_before_it_reads_the_rules(self):
        """Read outside the rename's transaction, a rule edited in between was written back
        over with its old search (renamed), and the edit silently lost."""
        import sqlite3
        rid = self.rule('grain=airings&f.tag=racing', name='Tagged')
        auto_rules.rename_in_rules('tag', 'racing', 'motorsport')
        other = sqlite3.connect(self.t.db_path, timeout=0.2)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                other.execute("UPDATE auto_rules SET name = 'edited' WHERE id = ?", (rid,))
        finally:
            db.session.rollback()
            other.close()


if __name__ == '__main__':
    unittest.main()
