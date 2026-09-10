"""Replaying a channel's health score from the observations it is still allowed to count.

`Channel.health_score` is a lossy exponential average (app/health_score.py::
blend_health_score), so nothing can be subtracted back out of it. Undoing an observation is
therefore always a full recompute from the ones that remain, in observation order - never an
increment (CLAUDE.md, "already done is a fact you recorded").

Two user actions are built on that, and they are the same mechanism twice:

  * **Reset** excludes every observation, leaving the channel with no score at all.
  * **Step back** excludes the single newest one that still counts, and can be clicked again.

Excluding, not deleting, is the deliberate call (dev/changelog/895). An observation is not
only a ChannelTest row - a terminal recording, a post-process damage correction, a group
failover and a stall demotion each move the score too - and deleting a Recording to unwind
its contribution would destroy the recording itself. So a `ChannelHealthExclusion` row marks
an observation as not-counted and every surface keeps showing it, marked.

**The ledger reads back the weights that were actually used.** Every blending path persists
its own `blend_breakdown` alongside the quality it produced, and that JSON carries the
`source_weight` the blend ran with, so a replay with nothing excluded reproduces the stored
score rather than re-deriving weights from durations that may since have been re-measured.
The `observation_weight()` fallback covers only the rows that predate those columns and the
capture-quality correction, which never stored its blend.

Two places a replay legitimately differs from the number history produced, both surfaced
rather than absorbed:

  1. **An observation that arrived out of order.** `blend_health_score` gives a stale
     observation zero weight, and a replay in timestamp order never sees one. That makes
     the replay the more honest of the two numbers.
  2. **An observation whose row has since been pruned.** Test retention deletes old
     `ChannelTest` rows, and a deleted row's contribution stays baked into the stored score
     with nothing left to replay it from. `rollback_preview()` reports the shortfall as
     `unledgered` and the confirm dialog says so, because a step-back that silently drops
     those observations' residual would otherwise move the score by an amount nothing on
     screen accounts for. It is small by construction - anything old enough to have been
     pruned is many half-lives back - and a full reset is unaffected, since it reaches "no
     observations count" either way.
"""
import json
import logging
from datetime import datetime
from typing import List

from sqlalchemy import or_

from .health_score import blend_health_score, observation_weight

log = logging.getLogger(__name__)

#: `ChannelHealthExclusion.source_kind` - which table (and which column of it) an
#: observation came from. A recording contributes up to TWO observations, so
#: SOURCE_RECORDING and SOURCE_CAPTURE_CORRECTION are separate kinds over the same id.
SOURCE_TEST = 'test'
SOURCE_RECORDING = 'recording'
SOURCE_CAPTURE_CORRECTION = 'capture_correction'
SOURCE_FAILOVER = 'failover'
SOURCE_STALL_DEMOTION = 'stall_demotion'

SOURCE_KINDS = (SOURCE_TEST, SOURCE_RECORDING, SOURCE_CAPTURE_CORRECTION,
                SOURCE_FAILOVER, SOURCE_STALL_DEMOTION)


class Observation:
    """One thing that moved (or would move) a channel's health score.

    `excluded` is the stored user decision; `quality` and `weight` are what the replay
    feeds blend_health_score. `label` is UI copy - it is what the confirm dialog and the
    ChannelEvent name, so it has to identify the observation to a human on its own.
    """

    __slots__ = ('kind', 'source_id', 'observed_at', 'quality', 'weight', 'label', 'excluded')

    def __init__(self, kind, source_id, observed_at, quality, weight, label, excluded=False):
        self.kind = kind
        self.source_id = source_id
        self.observed_at = observed_at
        self.quality = quality
        self.weight = weight
        self.label = label
        self.excluded = excluded

    @property
    def key(self):
        return (self.kind, self.source_id)


class _ScoreCarrier:
    """The three columns blend_health_score reads, without a Channel row behind them.

    blend_health_score is pure and takes the channel only to read its current score, so the
    replay hands it a running accumulator instead of mutating the real row mid-loop.
    """

    def __init__(self):
        self.health_score = None
        self.health_score_sample_count = 0
        self.health_score_updated_at = None


def _loads(raw):
    if not raw:
        return {}
    try:
        return json.loads(raw) or {}
    except (ValueError, TypeError):
        return {}


def _stored_weight(breakdown, fallback):
    """The source_weight a blend actually ran with, or `fallback` for a row that stored none."""
    weight = breakdown.get('source_weight')
    if isinstance(weight, (int, float)) and weight > 0:
        return float(weight)
    return fallback


def excluded_keys(channel_id):
    """{(kind, source_id)} the user has taken out of this channel's score."""
    from .database import ChannelHealthExclusion
    rows = ChannelHealthExclusion.query.filter_by(channel_id=channel_id).all()
    return {(r.source_kind, r.source_id) for r in rows}


def observation_ledger(channel_id, cfg) -> List[Observation]:
    """Every observation that has ever been blended into this channel's score, oldest first.

    Fixed query count regardless of how many observations a channel has - three queries,
    none of them per row (CLAUDE.md, no hidden I/O in per-row loops).
    """
    from .database import (ChannelEvent, ChannelTest, Recording,
                           CHANNEL_FAILOVER_HEALTH_OBSERVATION,
                           CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION)
    from .tz_utils import format_local

    excluded = excluded_keys(channel_id)
    obs: List[Observation] = []

    # A test that was never blended (CANCELLED, or still running) carries no quality_score,
    # which is exactly the "this one never counted" marker - see score_test_quality.
    tests = (ChannelTest.query
             .filter(ChannelTest.channel_id == channel_id,
                     ChannelTest.quality_score.isnot(None))
             .all())
    for t in tests:
        breakdown = _loads(t.blend_breakdown)
        weight = _stored_weight(breakdown, observation_weight(t.duration_seconds, cfg))
        obs.append(Observation(
            SOURCE_TEST, t.id, t.test_started_at, t.quality_score, weight,
            f'Health check on {format_local(t.test_started_at)} (scored {t.quality_score}/100)',
            (SOURCE_TEST, t.id) in excluded))

    # Either column alone is enough: the correction can exist on a row whose primary
    # observation was never written (its channel had already been re-observed by the time
    # postprocessing finished), and the primary exists long before the correction does.
    recordings = (Recording.query
                  .filter(Recording.channel_id == channel_id)
                  .filter(or_(Recording.health_quality_score.isnot(None),
                              Recording.capture_quality_breakdown.isnot(None)))
                  .all())
    for r in recordings:
        observed_at = r.completed_at or r.start_time
        if r.health_quality_score is not None:
            breakdown = _loads(r.health_blend_breakdown)
            quality_breakdown = _loads(r.health_quality_breakdown)
            share = quality_breakdown.get('member_share')
            duration = r.duration_seconds
            if isinstance(share, dict) and 'duration_seconds' in share:
                duration = share['duration_seconds']
            weight = _stored_weight(breakdown, observation_weight(duration, cfg))
            obs.append(Observation(
                SOURCE_RECORDING, r.id, observed_at, r.health_quality_score, weight,
                f'Recording "{r.name}" on {format_local(observed_at)} '
                f'(scored {r.health_quality_score}/100)',
                (SOURCE_RECORDING, r.id) in excluded))
        correction = _loads(r.capture_quality_breakdown)
        if correction.get('final') is not None:
            # The correction blends at postprocess time and stored no blend_breakdown, so
            # neither its weight nor its timestamp is on the row. Its weight is the config
            # constant it was read from, and it always follows its own recording's primary
            # observation - which is all the ordering a replay needs.
            rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
            obs.append(Observation(
                SOURCE_CAPTURE_CORRECTION, r.id, observed_at,
                correction['final'],
                float(rs_cfg.get('capture_quality_source_weight', 1.0)),
                f'Post-process check of recording "{r.name}" on {format_local(observed_at)} '
                f'(scored {correction["final"]}/100)',
                (SOURCE_CAPTURE_CORRECTION, r.id) in excluded))

    event_kinds = {
        CHANNEL_FAILOVER_HEALTH_OBSERVATION: (SOURCE_FAILOVER, 'Failed over away from this channel'),
        CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION: (SOURCE_STALL_DEMOTION,
                                                    'Moved off this channel for stalling'),
    }
    events = (ChannelEvent.query
              .filter(ChannelEvent.channel_id == channel_id,
                      ChannelEvent.event_type.in_(list(event_kinds)))
              .all())
    for e in events:
        kind, prose = event_kinds[e.event_type]
        extra = _loads(e.extra_data)
        quality = extra.get('quality')
        if quality is None:
            continue
        breakdown = extra.get('blend_breakdown') or {}
        weight = _stored_weight(breakdown, 1.0)
        obs.append(Observation(
            kind, e.id, e.timestamp, quality, weight,
            f'{prose} on {format_local(e.timestamp)} (scored {quality}/100)',
            (kind, e.id) in excluded))

    # A NULL timestamp cannot be ordered against a real one and would crash the sort; such a
    # row also cannot be placed in the replay, so it is dropped rather than guessed at.
    obs = [o for o in obs if o.observed_at is not None]
    obs.sort(key=lambda o: (o.observed_at, o.kind, o.source_id))
    return obs


def replay(ledger, cfg):
    """(score, sample_count, updated_at) from the observations in `ledger` that count.

    Returns (None, 0, None) when nothing counts - which is the whole point of a reset: a
    channel with no observations has no score, exactly as one that has never been tested.
    """
    carrier = _ScoreCarrier()
    counted = 0
    for o in ledger:
        if o.excluded:
            continue
        score, count, updated_at, _ = blend_health_score(
            carrier, o.quality, o.observed_at, o.weight, cfg)
        carrier.health_score = score
        carrier.health_score_sample_count = count
        carrier.health_score_updated_at = updated_at
        counted += 1
    if not counted:
        return None, 0, None
    return (carrier.health_score, carrier.health_score_sample_count,
            carrier.health_score_updated_at)


def recompute_failure_streak(channel_id, excluded=None):
    """`Channel.consecutive_test_failures` from the tests that still count.

    Recomputed rather than left alone, because it is a second score-adjacent signal
    (health_score.py::channel_failing_reason rule 2): a channel whose score was reset but
    whose streak still reads 3 is still reported as failing, which is the number-nobody-can-
    explain this feature exists to remove. Mirrors apply_test_health_observation's own rule -
    CANCELLED tests neither extend nor break a streak, so they are skipped entirely.
    """
    from .database import ChannelTest, TEST_STATUS_CANCELLED, TEST_STATUS_FAILED
    if excluded is None:
        excluded = excluded_keys(channel_id)
    tests = (ChannelTest.query
             .filter(ChannelTest.channel_id == channel_id,
                     ChannelTest.quality_score.isnot(None))
             .order_by(ChannelTest.test_started_at.desc(), ChannelTest.id.desc())
             .all())
    streak = 0
    for t in tests:
        if t.status == TEST_STATUS_CANCELLED:
            continue
        if (SOURCE_TEST, t.id) in excluded:
            continue
        if t.status == TEST_STATUS_FAILED:
            streak += 1
        else:
            break
    return streak


def rollback_preview(channel, cfg):
    """What the two rollback actions would do to `channel` right now, for the UI.

    Both numbers the confirm dialogs promise are computed here rather than in the template,
    so the sentence the user approves is produced by the same replay that will run.
    """
    from .channel_groups import effective_score

    ledger = observation_ledger(channel.id, cfg)
    counting = [o for o in ledger if not o.excluded]
    adjustment = channel.manual_health_adjustment or 0

    # Observations the stored score counted but nothing can replay - a pruned ChannelTest,
    # almost always. Reported so a step-back's projected number is explainable rather than
    # merely correct: silently dropping their residual is the number-nobody-can-account-for
    # this whole feature exists to remove.
    stored_count = channel.health_score_sample_count or 0
    preview = {
        'available': len(counting),
        'excluded_count': len(ledger) - len(counting),
        'unledgered': max(0, stored_count - len(ledger)),
        'manual_adjustment': adjustment,
        'current_score': None if channel.health_score is None else int(round(channel.health_score)),
        'current_effective': effective_score(channel) if channel.health_score is not None else None,
        'next': None,
    }
    if counting:
        newest = counting[-1]
        newest.excluded = True          # `ledger` is this call's own list, never shared
        score, count, _ = replay(ledger, cfg)
        preview['next'] = {
            'kind': newest.kind,
            'source_id': newest.source_id,
            'label': newest.label,
            'score_after': None if score is None else int(round(score)),
            'observations_after': count,
        }
    return preview


def _score_words(before, after):
    """"health score 34 -> 61", in the one spelling every rollback event uses."""
    b = 'none' if before is None else f'{before:.0f}'
    a = 'none' if after is None else f'{after:.0f}'
    return f'health score {b} -> {a}'


def apply_rollback(channel, action, cfg):
    """Exclude observations and rewrite the score. Mutates and adds; does NOT commit.

    The caller owns the commit so the whole read-modify-write stays inside one
    `retry_on_locked` unit (CLAUDE.md). `action` is 'reset' or 'step_back'.

    Returns a dict describing what happened, or None when there was nothing left to unwind.
    """
    from . import db
    from .database import (ChannelEvent, ChannelHealthExclusion,
                           CHANNEL_HEALTH_OVERRIDE_CHANGED, CHANNEL_HEALTH_ROLLBACK)

    ledger = observation_ledger(channel.id, cfg)
    counting = [o for o in ledger if not o.excluded]
    if not counting:
        return None

    if action == 'reset':
        targets = list(counting)
    else:
        targets = [counting[-1]]

    now = datetime.utcnow()
    for o in targets:
        db.session.add(ChannelHealthExclusion(
            channel_id=channel.id, source_kind=o.kind, source_id=o.source_id,
            excluded_at=now, action=action))
        o.excluded = True

    score, count, updated_at = replay(ledger, cfg)
    before = channel.health_score
    channel.health_score = score
    channel.health_score_sample_count = count
    channel.health_score_updated_at = updated_at
    channel.consecutive_test_failures = recompute_failure_streak(
        channel.id, excluded={o.key for o in ledger if o.excluded})

    # A reset means "as if this channel had never been observed", and a manual offset left
    # standing on a channel with no observations produces exactly the unexplainable number
    # this feature exists to remove - so it goes too, with its own event so the timeline
    # says both things moved. Step back leaves it alone: it unwinds one observation, and the
    # offset was never one.
    cleared_adjustment = None
    if action == 'reset' and (channel.manual_health_adjustment or channel.manual_health_note):
        cleared_adjustment = channel.manual_health_adjustment or 0
        db.session.add(ChannelEvent(
            channel_id=channel.id, timestamp=now,
            event_type=CHANNEL_HEALTH_OVERRIDE_CHANGED,
            detail=f'Manual adjustment changed {cleared_adjustment:+d} → +0 '
                   f'(cleared by a health score reset)',
            extra_data=json.dumps({
                'old_adjustment': cleared_adjustment, 'new_adjustment': 0,
                'old_note': channel.manual_health_note, 'new_note': None,
                'cleared_by': 'health_reset',
            })))
        channel.manual_health_adjustment = 0
        channel.manual_health_note = None

    if action == 'reset':
        detail = (f'Health score reset - {len(targets)} observation'
                  f'{"s" if len(targets) != 1 else ""} no longer counted, '
                  f'{_score_words(before, score)}')
        if cleared_adjustment:
            detail += f' (manual adjustment {cleared_adjustment:+d} cleared)'
    else:
        detail = (f'Stepped back one observation - {targets[0].label} no longer counted, '
                  f'{_score_words(before, score)}')

    db.session.add(ChannelEvent(
        channel_id=channel.id, timestamp=now, event_type=CHANNEL_HEALTH_ROLLBACK,
        detail=detail,
        extra_data=json.dumps({
            'action': action,
            'score_before': before, 'score_after': score,
            'observations_excluded': [{'kind': o.kind, 'source_id': o.source_id,
                                       'label': o.label} for o in targets],
            'observations_remaining': count,
            'cleared_manual_adjustment': cleared_adjustment,
        })))

    return {
        'action': action, 'detail': detail,
        'score_before': before, 'score_after': score,
        'excluded': len(targets), 'remaining': count,
        'cleared_manual_adjustment': cleared_adjustment,
    }
