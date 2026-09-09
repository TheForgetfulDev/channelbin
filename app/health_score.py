"""Channel lifetime health score: quality-score formulas + the shared decay-weighted blend.

Two independent observation sources feed the same per-channel score:
  - channel health tests (app/channel_tester.py, via apply_test_health_observation)
  - terminal recordings (app/watchdog.py, app/recorder.py, app/concatenator.py,
    app/postprocessor.py, via apply_recording_health_observation) - only the
    channel-attributable outcomes (max-consecutive-failures FAILED, no-valid-segments
    FAILED, and COMPLETED); local infra failures (disk space, missing DVR dir, mp4
    conversion errors) are not channel-quality signal and are never scored as failures.

    That rule is about the *failure* only. A capture that ran to the end and left
    segments on disk is channel signal regardless of what a later local step does with
    those segments, so the capture-phase observation is emitted once at the capture/concat
    boundary (apply_capture_phase_health_observation, called from app/concatenator.py) and
    every downstream outcome inherits it. Before that, a disk-space/concat/conversion
    failure discarded the whole capture's stalls and restarts - see dev/changelog/279.

Each source computes its own 0-100 "quality score" for a single observation
(score_test_quality / score_recording_success_quality), then blend_health_score folds it
into Channel.health_score via an incremental exponential-decay average - cached on Channel
(not recomputed from full history on every read) so it can be sorted/highlighted and so
recent observations naturally outweigh old ones without a second staleness formula.
"""
import json
import logging
from typing import Optional, Tuple

from .db_utils import retry_on_locked

log = logging.getLogger(__name__)

RECORDING_FAIL_FLOOR_DEFAULT = 5


def _clamp(value: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, value))


def score_test_quality(test, cfg: dict) -> Optional[Tuple[int, dict]]:
    """A single ChannelTest's 0-100 quality score, or None if it shouldn't count.

    ChannelTest.status is COMPLETED, FAILED, or CANCELLED. A CANCELLED test was aborted by
    something external to the channel - a recording reclaiming its connection slot, or the
    user stopping the run - so, like an ABORTED recording, it is excluded rather than scored.
    Scoring it would apply the fail floor and tank the score of a channel nothing is wrong
    with (see dev/docs/BUGS.md 2026-07-20 "preempted tests scored as FAILED").

    Returns (score, breakdown) where breakdown captures which penalties fired and why,
    for display on the channel detail page's activity timeline.
    """
    from .database import TEST_STATUS_CANCELLED, TEST_STATUS_FAILED, TEST_STATUS_COMPLETED

    hs_cfg = cfg.get('channel_testing', {})
    if test.status == TEST_STATUS_CANCELLED:
        return None
    if test.status == TEST_STATUS_FAILED:
        fail_floor = hs_cfg.get('health_score_test_fail_floor', 10)
        breakdown = {
            'base': None, 'fail_floor_applied': True, 'fail_floor_value': fail_floor,
            'penalties': [], 'final': fail_floor,
        }
        return fail_floor, breakdown
    if test.status != TEST_STATUS_COMPLETED:
        return None

    # Frame-pct shortfall converted to affected-seconds and scored via the same
    # proportional-loss + instability formula a recording uses (decision: fix the
    # identical duration-blindness bug on the test side in the same pass - a 30-minute
    # test racking up more "drops" than a 2-minute test purely by having more
    # opportunities to used to score the same as a proportionally-worse 2-minute test).
    duration = test.duration_seconds or 0
    if test.frame_pct is not None and duration:
        affected_seconds = duration * max(0.0, 1 - test.frame_pct / 100.0)
    else:
        affected_seconds = 0.0
    final, breakdown = score_recording_metrics_quality(
        affected_seconds, test.drop_count or 0, duration, cfg)
    score = float(final)
    penalties = breakdown['penalties']

    if test.error_detail:
        # A WARN embedded in an otherwise-COMPLETED test (short recording, uniform
        # screenshot, etc.) - a flat penalty since the specific cause varies. Categorical,
        # not duration-blind, so it stays a flat penalty rather than joining the formula above.
        amount = -hs_cfg.get('health_score_warn_penalty', 10)
        score += amount
        penalties.append({'reason': 'warn', 'detail': test.error_detail, 'amount': amount})

    final = round(_clamp(score))
    breakdown = {'base': breakdown['base'], 'fail_floor_applied': False, 'penalties': penalties, 'final': final}
    return final, breakdown


def score_recording_success_quality(recording, cfg: dict) -> Tuple[int, dict]:
    """A COMPLETED recording's 0-100 quality score, from its stall/restart history.

    Real-world usage over a much longer window than a synthetic test - a recording that
    "succeeded" but stalled/restarted repeatedly still indicates real degradation the
    watchdog's auto-recovery masked, which a pass/fail test alone can't see.

    Thin wrapper over score_recording_metrics_quality using the recording's cumulative
    counters - the right input for a single-member recording and for a group's own
    (whole-recording) score. For per-member attribution of a group recording that failed
    over, callers compute a member's share and call score_recording_metrics_quality directly.

    Returns (score, breakdown) - see score_test_quality for the shared breakdown shape.
    """
    return score_recording_metrics_quality(
        recording.total_downtime_seconds or 0,
        recording.total_restart_count or 0,
        recording.duration_seconds or 0,
        cfg,
    )


def observation_weight(duration_seconds: float, cfg: dict) -> float:
    """How much a single observation counts in blend_health_score(), scaled by its own
    duration (sqrt, diminishing returns) instead of a flat per-source-type multiplier.

    Replaces the old flat source_weight (recording=3, test=1): a 30-minute test observed
    twice as much real time as a 15-minute recording and should outweigh it, not
    automatically lose 3-to-1. reference_minutes is the duration that gets weight 1.0 -
    default 2, matching the real test_duration_seconds default (120s).
    """
    ref = cfg.get('channel_testing', {}).get('reference_minutes', 2)
    minutes = max((duration_seconds or 0) / 60.0, 0.1)
    return (minutes / max(ref, 0.01)) ** 0.5


def score_recording_metrics_quality(downtime_seconds: float, restart_count: int,
                                    duration_seconds: float, cfg: dict) -> Tuple[int, dict]:
    """0-100 quality score for a set of recording metrics (time lost + restart frequency
    over a duration). Factored out of score_recording_success_quality so the same formula
    scores either a whole recording or one group member's share of it, and is reused
    directly by score_test_quality (a test is scored the same way).

    Quality is proportional to how much of the window was actually lost
    (downtime_seconds / duration_seconds), not a flat penalty per stall/restart event -
    a recording that lost 1.58% of its content to 19 successfully-recovered restarts no
    longer scores an 8/100 indistinguishable from a total failure. A separate instability
    term penalizes event *frequency* on top of that, so many short interruptions score
    worse than one contiguous outage of the same total length.

    Returns (score, breakdown) - see score_test_quality for the shared breakdown shape.
    """
    window = max(duration_seconds or 0, 1.0)
    hours = max(window / 3600.0, 0.01)
    affected_fraction = min(max(downtime_seconds or 0, 0.0) / window, 1.0)
    base = 100.0 * (1 - affected_fraction)
    instability_per_hr = cfg.get('channel_testing', {}).get('instability_penalty_per_hr', 2.0)
    instability = (restart_count or 0) / hours * instability_per_hr

    penalties = []
    if downtime_seconds:
        penalties.append({'reason': 'time_lost',
                           'detail': f'{downtime_seconds:.0f}s lost of {window:.0f}s '
                                     f'({affected_fraction * 100:.1f}%)',
                           'amount': -(affected_fraction * 100)})
    if restart_count:
        penalties.append({'reason': 'instability',
                           'detail': f'{restart_count} restarts / {hours:.2f}h × {instability_per_hr}/hr',
                           'amount': -instability})

    final = round(_clamp(base - instability))
    breakdown = {'base': round(base, 1), 'fail_floor_applied': False, 'penalties': penalties, 'final': final}
    return final, breakdown


def blend_health_score(channel, quality: int, observed_at, source_weight: float, cfg: dict):
    """Fold one observation into a channel's cached lifetime score. Pure - no I/O.

    Returns (new_score, new_sample_count, new_updated_at, breakdown). First-ever observation
    is taken as-is. Otherwise blends via a fixed per-observation decay (count-based, not
    time-based): every individual observation gets the same weight regardless of how soon
    it arrives after the last one, so a channel that fails every day for weeks reflects that
    within a handful of observations instead of taking weeks to move past a long half-life.
    source_weight makes a single observation count as if it were blended in `source_weight`
    times in a row (e.g. a long recording outweighing a short test) - see observation_weight().

    observed_at older than the channel's current health_score_updated_at (e.g. a recording
    whose slow postprocessing pipeline finalizes after a more recent test already updated
    the score) contributes zero weight - handled by an explicit stale check, since
    count-based decay has no elapsed-time term to fall back on for this - but must NOT move
    health_score_updated_at backwards, or a later real observation would look stale too.
    """
    if channel.health_score is None:
        breakdown = {
            'quality': quality, 'source_weight': source_weight, 'first_observation': True,
            'new_score': float(quality),
        }
        return float(quality), 1, observed_at, breakdown

    old_score = channel.health_score
    existing_updated_at = channel.health_score_updated_at
    half_life_samples = cfg.get('channel_testing', {}).get('health_score_half_life_samples', 5)

    if existing_updated_at and observed_at < existing_updated_at:
        effective_alpha = 0.0
        decay = 1.0
    else:
        decay = 0.5 ** (1.0 / half_life_samples) if half_life_samples > 0 else 0.0
        effective_alpha = 1 - decay ** source_weight

    new_score = old_score * (1 - effective_alpha) + quality * effective_alpha
    new_count = (channel.health_score_sample_count or 0) + 1
    new_updated_at = observed_at
    if existing_updated_at and existing_updated_at > observed_at:
        new_updated_at = existing_updated_at

    breakdown = {
        'quality': quality, 'source_weight': source_weight, 'first_observation': False,
        'old_score': old_score, 'observed_at': observed_at.isoformat() if hasattr(observed_at, 'isoformat') else str(observed_at),
        'half_life_samples': half_life_samples,
        'decay': round(decay, 4), 'effective_alpha': round(effective_alpha, 4),
        'new_score': new_score,
    }
    return new_score, new_count, new_updated_at, breakdown


@retry_on_locked()
def apply_test_health_observation(app, test_id: int):
    """Blend a just-finalized ChannelTest's quality score into its channel's score.

    Its own commit - called after _finalize_test's commit has already succeeded, never
    combined with it into one multi-commit retry unit (CLAUDE.md commit discipline).
    """
    with app.app_context():
        from . import db
        from .config import load_config
        from .database import ChannelTest, Channel, TEST_STATUS_FAILED, TEST_STATUS_COMPLETED

        test = db.session.get(ChannelTest, test_id)
        if test is None:
            return
        cfg = load_config()
        result = score_test_quality(test, cfg)
        if result is None:
            return
        quality, quality_breakdown = result

        channel = db.session.get(Channel, test.channel_id)
        if channel is None:
            return

        # Consecutive-failure streak (dev/changelog/478) - a signal distinct from the
        # score blended below, see channel_groups.is_streaking / channel_failing_reason.
        # CANCELLED already returned above (score_test_quality excludes it), so this only
        # ever sees FAILED (increment) or COMPLETED (reset) - the streak is neither
        # extended nor broken by a test that wasn't the channel's fault.
        if test.status == TEST_STATUS_FAILED:
            channel.consecutive_test_failures = (channel.consecutive_test_failures or 0) + 1
        elif test.status == TEST_STATUS_COMPLETED:
            channel.consecutive_test_failures = 0

        weight = observation_weight(test.duration_seconds, cfg)
        new_score, new_count, new_updated_at, blend_breakdown = blend_health_score(
            channel, quality, test.test_started_at, weight, cfg
        )
        channel.health_score = new_score
        channel.health_score_sample_count = new_count
        channel.health_score_updated_at = new_updated_at
        test.quality_score = quality
        test.lifetime_score_after = round(new_score)
        test.quality_breakdown = json.dumps(quality_breakdown)
        test.blend_breakdown = json.dumps(blend_breakdown)
        db.session.commit()


@retry_on_locked()
def apply_failover_health_observation(app, channel_id: int, recording_id: int, reason: str = ''):
    """Blend a fail-floor observation into a group member abandoned mid-recording
    by group failover (app/recorder.py::failover_group_member) - its feed
    demonstrably died while being recorded, which is exactly the signal the
    recording fail floor represents. Unlike apply_recording_health_observation
    this never touches the Recording row: the recording itself is still running
    (on another member) and gets its own terminal observation later.

    Also writes a CHANNEL_FAILOVER_HEALTH_OBSERVATION ChannelEvent on the abandoned
    channel - without it this score hit was a silent DB mutation with no entry on the
    channel's own Activity Timeline (dev/docs/BUGS.md 2026-08-10).
    """
    with app.app_context():
        import json
        from . import db
        from .config import load_config
        from .database import Channel, Recording, ChannelEvent, CHANNEL_FAILOVER_HEALTH_OBSERVATION
        from datetime import datetime

        channel = db.session.get(Channel, channel_id)
        if channel is None:
            return
        cfg = load_config()
        rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
        quality = rs_cfg.get('fail_floor', RECORDING_FAIL_FLOOR_DEFAULT)
        # No finished-observation duration exists yet (the recording is still running on
        # another member) - weight on how long this member was actually being recorded
        # for, i.e. real usage lost, not a flat multiplier.
        recording = db.session.get(Recording, recording_id)
        elapsed = 0.0
        if recording is not None and recording.start_time is not None:
            elapsed = max((datetime.utcnow() - recording.start_time).total_seconds(), 0.0)
        weight = observation_weight(elapsed, cfg)
        observed_at = datetime.utcnow()
        new_score, new_count, new_updated_at, blend_breakdown = blend_health_score(
            channel, quality, observed_at, weight, cfg
        )
        channel.health_score = new_score
        channel.health_score_sample_count = new_count
        channel.health_score_updated_at = new_updated_at

        if blend_breakdown['first_observation']:
            score_note = f'score set to {new_score:.0f}'
        else:
            score_note = f'health score {blend_breakdown["old_score"]:.0f} -> {new_score:.0f}'
        detail = (f'Feed died mid-recording ({reason}) and failed over to another group '
                  f'member - {score_note}')
        db.session.add(ChannelEvent(
            channel_id=channel_id,
            timestamp=observed_at,
            event_type=CHANNEL_FAILOVER_HEALTH_OBSERVATION,
            detail=detail,
            extra_data=json.dumps({
                'recording_id': recording_id, 'reason': reason,
                'quality': quality, 'blend_breakdown': blend_breakdown,
            }),
        ))
        db.session.commit()


def _departed_member_share(recording_id: int):
    """(downtime_seconds, restart_count, duration_seconds) for the member a recording has
    JUST moved off, read from its GROUP_FAILOVER events.

    Called after failover_group_member has committed its event, so the newest one carries
    `counters_at_failover` = the recording's cumulative counters through the departing
    member, and the one before it (if any) marks where that member's window started. The
    difference is the member's own share - the same subtraction _final_member_success_quality
    does for the member a recording ENDS on, one event earlier.

    Requires an app context. Returns None when the newest event carries no snapshot, which
    is the caller's signal that there is nothing measured to score.
    """
    from .database import RecordingEvent, GROUP_FAILOVER, Recording
    from . import db

    events = (RecordingEvent.query
              .filter_by(recording_id=recording_id, event_type=GROUP_FAILOVER)
              .order_by(RecordingEvent.timestamp.desc(), RecordingEvent.id.desc())
              .limit(2).all())
    if not events:
        return None

    def _snap(ev):
        if ev is None or not ev.extra_data:
            return None
        try:
            return json.loads(ev.extra_data).get('counters_at_failover')
        except (ValueError, TypeError):
            return None

    latest = _snap(events[0])
    if not latest:
        return None
    prior = _snap(events[1]) if len(events) > 1 else None
    start_counters = prior or {}

    downtime = max(0.0, (latest.get('total_downtime_seconds') or 0)
                   - (start_counters.get('total_downtime_seconds') or 0))
    restarts = max(0, (latest.get('total_restart_count') or 0)
                   - (start_counters.get('total_restart_count') or 0))

    # Window start: the previous failover, or the recording's own start for the first
    # member. Both ends come from event timestamps rather than utcnow() so a slow commit
    # cannot stretch the window and flatter the member's instability rate.
    start_at = None
    if len(events) > 1 and prior is not None:
        start_at = events[1].timestamp
    if start_at is None:
        recording = db.session.get(Recording, recording_id)
        start_at = recording.start_time if recording is not None else None
    duration = 0.0
    if start_at is not None and events[0].timestamp is not None:
        duration = max((events[0].timestamp - start_at).total_seconds(), 0.0)
    return downtime, restarts, duration


@retry_on_locked()
def apply_stall_demotion_health_observation(app, channel_id: int, recording_id: int,
                                            reason: str = ''):
    """Blend a MEASURED observation into a group member a recording moved off because it
    kept stalling (app/recorder.py::failover_group_member with demote=True).

    Deliberately not apply_failover_health_observation, which hands the channel the flat
    recording fail floor. That is the right answer for a feed that died and the wrong one
    here: the member that motivated this feature delivered 104% of its expected content
    across 27 stalls, so a fail floor would have been a lie about a working feed. This
    scores it through score_recording_metrics_quality on its own share instead, where the
    stalls still hit both terms - every stall banks its dead air into downtime and forces
    a restart the instability term charges for - so a member that stalls more scores worse,
    proportionally rather than categorically (dev/changelog/889).

    Never touches the Recording row: the recording is still running on another member and
    gets its own terminal observation later.
    """
    with app.app_context():
        from . import db
        from .config import load_config
        from .database import (Channel, ChannelEvent,
                               CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION)
        from datetime import datetime

        channel = db.session.get(Channel, channel_id)
        if channel is None:
            return
        share = _departed_member_share(recording_id)
        if share is None:
            # No snapshot means nothing measured to score. Silence here is safe: the
            # demotion itself is already on the recording's event log, and inventing a
            # score from counters that span other members would be worse than none.
            log.warning('Recording %d: no failover snapshot for channel %d - stall '
                        'demotion recorded no health observation', recording_id, channel_id)
            return
        downtime, restarts, duration = share

        cfg = load_config()
        quality, quality_breakdown = score_recording_metrics_quality(
            downtime, restarts, duration, cfg)
        quality_breakdown['member_share'] = {
            'reason': 'stall-rate demotion - departing member share only',
            'downtime_seconds': round(downtime), 'restarts': restarts,
            'duration_seconds': round(duration),
        }
        weight = observation_weight(duration, cfg)
        observed_at = datetime.utcnow()
        new_score, new_count, new_updated_at, blend_breakdown = blend_health_score(
            channel, quality, observed_at, weight, cfg
        )
        channel.health_score = new_score
        channel.health_score_sample_count = new_count
        channel.health_score_updated_at = new_updated_at

        if blend_breakdown['first_observation']:
            score_note = f'score set to {new_score:.0f}'
        else:
            score_note = f'health score {blend_breakdown["old_score"]:.0f} -> {new_score:.0f}'
        detail = (f'Kept stalling during a recording ({reason}) - moved to another group '
                  f'member and demoted for the rest of that recording. Scored {quality}/100 '
                  f'on its own share: {downtime:.0f}s lost and {restarts} restarts over '
                  f'{duration / 60:.0f} min - {score_note}')
        db.session.add(ChannelEvent(
            channel_id=channel_id,
            timestamp=observed_at,
            event_type=CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION,
            detail=detail,
            extra_data=json.dumps({
                'recording_id': recording_id, 'reason': reason,
                'quality': quality, 'quality_breakdown': quality_breakdown,
                'blend_breakdown': blend_breakdown,
            }),
        ))
        db.session.commit()


def _final_member_success_quality(recording, cfg: dict) -> Tuple[int, dict]:
    """Quality score for the channel a COMPLETED recording *ended* on.

    For a group-backed recording that failed over, the recording's cumulative
    downtime/restart counters span every member it went through - blaming the final
    feed for downtime on the dead feeds it escaped. Score the final member from only its
    own share (cumulative minus the snapshot stored in the last GROUP_FAILOVER event).
    Non-group recordings, and group recordings that never failed over, score from the
    whole recording (share == cumulative) - identical to the pre-fix behavior.
    """
    from .database import RecordingEvent, GROUP_FAILOVER
    from datetime import datetime

    if recording.group_id is None:
        return score_recording_success_quality(recording, cfg)

    last = (RecordingEvent.query
            .filter_by(recording_id=recording.id, event_type=GROUP_FAILOVER)
            .order_by(RecordingEvent.timestamp.desc(), RecordingEvent.id.desc())
            .first())
    snap = None
    if last is not None and last.extra_data:
        try:
            snap = json.loads(last.extra_data).get('counters_at_failover')
        except (ValueError, TypeError):
            snap = None
    if not snap:
        return score_recording_success_quality(recording, cfg)

    downtime = max(0.0, (recording.total_downtime_seconds or 0) - (snap.get('total_downtime_seconds') or 0))
    restarts = max(0, (recording.total_restart_count or 0) - (snap.get('total_restart_count') or 0))
    # Final member's active window: from the last failover to the end of capture. The
    # observation is emitted at the capture/concat boundary, before completed_at exists, so
    # utcnow() is the end of that window; falling back to duration_seconds (the whole
    # scheduled span, including every dead member before the failover) would understate the
    # instability rate. completed_at is still honored when the caller runs after the row finished.
    duration = recording.duration_seconds or 0
    if last.timestamp is not None:
        end = recording.completed_at or datetime.utcnow()
        duration = max((end - last.timestamp).total_seconds(), 0)

    quality, breakdown = score_recording_metrics_quality(downtime, restarts, duration, cfg)
    breakdown['member_share'] = {
        'reason': 'group failover - final member share only',
        'downtime_seconds': round(downtime), 'restarts': restarts,
        'duration_seconds': round(duration),
    }
    return quality, breakdown


def apply_recording_health_observation(app, recording_id: int, outcome: str):
    """Blend a just-terminated recording's quality into its channel's score, and - for a
    group-backed recording - into the group's own score too.

    outcome: 'success' (COMPLETED) or 'failed' (a channel-attributable FAILED - caller is
    responsible for only invoking this for outcomes that actually reflect stream quality,
    not local infra failures). Called after the recording's own status-transition commit
    has already succeeded - never combined with it.

    The channel blend and the group blend each get their own retry_on_locked commit closure
    (CLAUDE.md: never two commits in one whole-function retry unit).
    """
    with app.app_context():
        from . import db
        from .config import load_config
        from .database import Recording, Channel, ChannelGroup
        from datetime import datetime

        recording = db.session.get(Recording, recording_id)
        if recording is None:
            return

        cfg = load_config()
        rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
        observed_at = recording.completed_at or datetime.utcnow()
        fail_floor = rs_cfg.get('fail_floor', RECORDING_FAIL_FLOOR_DEFAULT)

        # ── Channel-level observation: the feed the recording ended on ──
        if recording.channel_id is not None:
            channel_duration = recording.duration_seconds or 0
            if outcome == 'success':
                quality, quality_breakdown = _final_member_success_quality(recording, cfg)
                member_share = quality_breakdown.get('member_share')
                if isinstance(member_share, dict) and 'duration_seconds' in member_share:
                    channel_duration = member_share['duration_seconds']
            else:
                quality = fail_floor
                quality_breakdown = {
                    'base': None, 'fail_floor_applied': True, 'fail_floor_value': quality,
                    'penalties': [], 'final': quality,
                }
            channel_weight = observation_weight(channel_duration, cfg)

            @retry_on_locked()
            def _blend_channel_and_commit():
                channel = db.session.get(Channel, recording.channel_id)
                if channel is None:
                    return
                new_score, new_count, new_updated_at, blend_breakdown = blend_health_score(
                    channel, quality, observed_at, channel_weight, cfg
                )
                channel.health_score = new_score
                channel.health_score_sample_count = new_count
                channel.health_score_updated_at = new_updated_at
                rec = db.session.get(Recording, recording_id)
                rec.health_quality_score = quality
                rec.health_quality_breakdown = json.dumps(quality_breakdown)
                rec.health_blend_breakdown = json.dumps(blend_breakdown)
                db.session.commit()

            _blend_channel_and_commit()

        # ── Group-level observation: total-churn health of the group as a whole ──
        # The whole-recording cumulative quality (every member's stalls/restarts combined)
        # is exactly "how much churn it took the group to deliver" - see CLAUDE.md ChannelGroup.
        if recording.group_id is not None:
            if outcome == 'success':
                group_quality, _ = score_recording_success_quality(recording, cfg)
            else:
                group_quality = fail_floor
            group_weight = observation_weight(recording.duration_seconds or 0, cfg)

            @retry_on_locked()
            def _blend_group_and_commit():
                group = db.session.get(ChannelGroup, recording.group_id)
                if group is None:
                    return
                new_score, new_count, new_updated_at, _ = blend_health_score(
                    group, group_quality, observed_at, group_weight, cfg
                )
                group.health_score = new_score
                group.health_score_sample_count = new_count
                group.health_score_updated_at = new_updated_at
                db.session.commit()

            _blend_group_and_commit()


def apply_capture_phase_health_observation(app, recording_id: int):
    """Blend a just-finished capture's quality in, independent of what happens next.

    Called once at the capture/concat boundary (app/concatenator.py, as soon as the
    capture is known to have left usable segments on disk). Everything after that point -
    concat, mp4 conversion, move, post-script - is local work whose failure is not channel
    signal, but which used to take the capture's stalls/restarts down with it because the
    only observation on this pipeline sat at the very end of post-processing.

    Idempotent by design: health_quality_score on the row is the "this capture has already
    been observed" marker, so a Retry concat / Retry conversion cannot blend the same
    capture into the channel score a second time.

    Deliberately NOT reached by a user-aborted capture: abort_recording never concatenates,
    so ABORTED stays excluded exactly as before.
    """
    with app.app_context():
        from . import db
        from .database import Recording

        recording = db.session.get(Recording, recording_id)
        if recording is None:
            return
        if recording.health_quality_score is not None:
            log.debug('Recording %d already carries a health observation - not re-blending',
                      recording_id)
            return

    apply_recording_health_observation(app, recording_id, 'success')


def score_capture_quality_correction(timeline_deficit_seconds: float, near_empty_seconds: float,
                                     span_seconds: float, cfg: dict) -> Tuple[int, dict]:
    """Pure: 0-100 quality for the postprocess damage/near-empty correction bolt-on.

    Deliberately scoped to ONLY timeline_deficit_seconds and near_empty_seconds - never
    total_downtime_seconds or restart counts, which the primary capture-phase observation
    (apply_recording_health_observation) already fully accounts for. Re-reading those here
    would double-count the same lost time under a different name; this formula only ever
    sees information the primary observation never had (it runs before the file is probed).
    Quality is 100 when nothing was found - itself positive evidence under count-based decay.
    """
    rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
    span = span_seconds or 0
    missing_pct = (timeline_deficit_seconds or 0) / span * 100 if span else 0.0
    near_empty_pct = (near_empty_seconds or 0) / span * 100 if span else 0.0
    damage_penalty = missing_pct * rs_cfg.get('damage_penalty_per_pct_missing', 3)
    near_empty_penalty = near_empty_pct * rs_cfg.get('near_empty_penalty_per_pct', 2)
    quality = round(_clamp(100 - damage_penalty - near_empty_penalty))
    breakdown = {
        'base': 100, 'fail_floor_applied': False, 'final': quality,
        'penalties': [
            p for p in [
                {'reason': 'timeline_damage',
                 'detail': f'{timeline_deficit_seconds or 0:.0f}s missing of {span:.0f}s '
                           f'({missing_pct:.1f}%)', 'amount': -damage_penalty} if missing_pct else None,
                {'reason': 'near_empty',
                 'detail': f'{near_empty_seconds or 0:.0f}s near-empty of {span:.0f}s '
                           f'({near_empty_pct:.1f}%)', 'amount': -near_empty_penalty} if near_empty_pct else None,
            ] if p is not None
        ],
    }
    return quality, breakdown


@retry_on_locked()
def apply_capture_quality_correction(app, recording_id: int):
    """Blend a small, second observation once postprocessing has measured timeline damage
    and near-empty/slate content - information the primary capture-phase observation
    (apply_recording_health_observation) never had, since it runs before the file is probed.
    Always called (not gated on damage found) - see score_capture_quality_correction().
    """
    with app.app_context():
        from . import db
        from .config import load_config
        from .database import Channel, Recording
        from datetime import datetime

        recording = db.session.get(Recording, recording_id)
        if recording is None or recording.channel_id is None:
            return
        channel = db.session.get(Channel, recording.channel_id)
        if channel is None:
            return

        cfg = load_config()
        rs_cfg = cfg.get('channel_testing', {}).get('recording_score', {})
        quality, breakdown = score_capture_quality_correction(
            recording.timeline_deficit_seconds, recording.near_empty_seconds,
            recording.duration_seconds, cfg)

        weight = rs_cfg.get('capture_quality_source_weight', 1.0)
        new_score, new_count, new_updated_at, _ = blend_health_score(
            channel, quality, datetime.utcnow(), weight, cfg
        )
        channel.health_score = new_score
        channel.health_score_sample_count = new_count
        channel.health_score_updated_at = new_updated_at
        recording.capture_quality_breakdown = json.dumps(breakdown)
        db.session.commit()


def channel_failing_reason(test, channel, cfg) -> Optional[str]:
    """Human-readable reason this channel should be considered failing for recording
    purposes, or None if it is fine (DESIGN-prerecord-checks.md §1, streak rule added
    dev/changelog/478).

    `test` is the just-finalized ChannelTest, or None when evaluating from the channel's
    current state alone (the group rule, and the schedule-time check - see
    assess_scheduled_recording_impact / evaluate_and_alert_recording). A CANCELLED test
    hits neither rule 1 nor rule 2 below: rule 1 only fires on a literal 'FAILED' status,
    and rule 2 reads the channel's current effective_score, which a CANCELLED test never
    touched (score_test_quality excludes it from blending). Rule 3 (the streak) is
    channel-level state, not test-dependent, so it fires the same way regardless of `test`.

    Rules, in order:
      1. The just-finished test hard-failed.
      2. consecutive_test_failures >= failing_streak_threshold - a channel with a good
         prior history can sit well above the score threshold through weeks of hard
         failures (blend_health_score decays by time-since-observation, not by streak
         length); this is a deliberately separate signal, never folded into effective_score
         (CLAUDE.md "one flag, one meaning").
      3. effective_score bands at or below `channel_testing.failing_band`.

    Rule 3 is declared as a band rather than a raw score (dev/changelog/771) so the warning
    speaks the same vocabulary the badges do; the numeric cut point is derived from the
    band's cut points, never stored a second time.
    """
    from .database import TEST_STATUS_FAILED
    if test is not None and test.status == TEST_STATUS_FAILED:
        return f'health check failed: {test.error_detail or "no data received"}'

    from .channel_groups import effective_score, is_streaking, DEFAULT_FAILING_STREAK_THRESHOLD
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    if is_streaking(channel, streak_threshold):
        return (f'failed its last {channel.consecutive_test_failures} consecutive '
                f'health checks')

    from .health_bands import (BAND_NAMES, band_by_key, band_for, failing_band_key,
                               failing_threshold, resolve_bands)
    bands = resolve_bands(cfg)
    threshold = failing_threshold(cfg, bands)
    if threshold is None:
        return None
    score = effective_score(channel)
    if score < threshold:
        band = band_by_key(bands, band_for(score, bands))
        band_label = band.label if band is not None else 'unbanded'
        return (f'effective health score {score} is {band_label} - '
                f'{BAND_NAMES[failing_band_key(cfg)]} or below counts as failing')
    return None


def _apply_failing_alert(rec, name: str, reason: Optional[str], source: str, is_group: bool):
    """Create-or-dismiss the standing RECORDING_CHANNEL_FAILING alert for one
    (recording, channel-or-group) pair, keyed by `source`. Dedupe/auto-dismiss mirror
    app/channel_groups.py::_log_and_alert_reconcile's GROUP_FORMAT_MISMATCH pattern."""
    from . import db
    from .database import Alert
    from .tz_utils import format_local
    from datetime import datetime

    if reason is not None:
        exists = Alert.query.filter_by(
            alert_type='RECORDING_CHANNEL_FAILING', source=source, dismissed_at=None
        ).first()
        if exists is not None:
            return
        from .alerts import create_alert
        label = 'Group' if is_group else 'Channel'
        create_alert(
            'RECORDING_CHANNEL_FAILING',
            f'Scheduled recording channel failing: {rec.name}',
            body=(f'{label} "{name}" backing scheduled recording "{rec.name}" '
                  f'(starts {format_local(rec.start_time)}) is failing: {reason}.'),
            source=source, recording_id=rec.id)
    else:
        @retry_on_locked()
        def _dismiss():
            for a in Alert.query.filter_by(
                    alert_type='RECORDING_CHANNEL_FAILING', source=source, dismissed_at=None
            ).all():
                a.dismissed_at = datetime.utcnow()
            db.session.commit()
        _dismiss()


def _group_failing_reason(group, cfg) -> Optional[str]:
    """Group-level failing reason for whichever member record-start would pick right
    now - score/streak rule only (test=None), since there is no single "just-finished
    test" for a whole group. Shared by evaluate_and_alert_recording (creation-time
    check) and assess_scheduled_recording_impact's group loop below."""
    from .channel_groups import pick_best_member, recording_members, DEFAULT_FAILING_STREAK_THRESHOLD
    from .routes.channel_tests import _latest_tests_by_channel
    streak_threshold = cfg.get('channel_testing', {}).get(
        'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
    members = recording_members(group.memberships)
    latest_by_channel = _latest_tests_by_channel([ch.id for ch in members])
    best = pick_best_member(members, latest_by_channel, streak_threshold=streak_threshold)
    if best is None:
        return 'no active channel available in the group'
    return channel_failing_reason(None, best, cfg)


def evaluate_and_alert_recording(rec, cfg):
    """Evaluate whether `rec`'s current channel/group is failing right now (score/streak
    rule only - no fresh test) and raise-or-clear the standing RECORDING_CHANNEL_FAILING
    alert for it. This is the creation-time half of DESIGN-prerecord-checks.md §2 - the
    schedule-time gap the streak rule closes (dev/changelog/478): a recording created
    directly onto an already-streaking channel previously got no warning until the next
    scheduled test happened to run. Called from new_recording_json/new_recording right
    after the recording is created; SCHEDULED-only by construction since it never runs on
    anything else. Best-effort like its reactive sibling below - never raises."""
    try:
        if rec.group_id is not None and rec.group is not None:
            reason = _group_failing_reason(rec.group, cfg)
            _apply_failing_alert(rec, rec.group.name, reason,
                                 source=f'recfail:rec:{rec.id}:grp:{rec.group_id}', is_group=True)
        elif rec.channel_id is not None:
            from . import db
            from .database import Channel
            channel = db.session.get(Channel, rec.channel_id)
            if channel is not None:
                reason = channel_failing_reason(None, channel, cfg)
                _apply_failing_alert(rec, channel.name, reason,
                                     source=f'recfail:rec:{rec.id}:ch:{rec.channel_id}', is_group=False)
    except Exception:
        log.exception('evaluate_and_alert_recording failed for recording %d', rec.id)


def assess_scheduled_recording_impact(app, test_id: int):
    """Reactive half of DESIGN-prerecord-checks.md §2: after a channel test finalizes and
    its score observation is blended in, raise or clear RECORDING_CHANNEL_FAILING alerts
    for any SCHEDULED recording that channel (or a group it belongs to)
    backs. Best-effort - never breaks the calling test run (mirrors
    app/channel_tester.py::_recheck_group_format's guard)."""
    with app.app_context():
        try:
            from . import db
            from .config import load_config
            from .database import ChannelTest, Channel, Recording, REC_STATUS_SCHEDULED

            test = db.session.get(ChannelTest, test_id)
            if test is None:
                return
            channel = db.session.get(Channel, test.channel_id)
            if channel is None:
                return
            cfg = load_config()

            # Direct: group_id must be NULL - a group-backed recording also carries a
            # frozen channel_id (the member picked at record creation), and the
            # established identity precedence in this codebase is group > channel (see
            # app/routes/guide.py::_build_rec_indexes). Without this filter a
            # group-backed recording would be wrongly judged by one member's own hard
            # failure instead of the group rule below.
            reason = channel_failing_reason(test, channel, cfg)
            direct_recs = Recording.query.filter_by(
                status=REC_STATUS_SCHEDULED, channel_id=channel.id, group_id=None).all()
            for rec in direct_recs:
                _apply_failing_alert(
                    rec, channel.name, reason,
                    source=f'recfail:rec:{rec.id}:ch:{channel.id}', is_group=False)

            group_ids = {m.group_id for m in channel.group_memberships}
            if group_ids:
                group_recs = Recording.query.filter(
                    Recording.status == REC_STATUS_SCHEDULED,
                    Recording.group_id.in_(group_ids)).all()
                by_group = {}
                for rec in group_recs:
                    by_group.setdefault(rec.group_id, []).append(rec)
                for group_id, recs in by_group.items():
                    group = recs[0].group
                    grp_reason = _group_failing_reason(group, cfg)
                    for rec in recs:
                        _apply_failing_alert(
                            rec, group.name, grp_reason,
                            source=f'recfail:rec:{rec.id}:grp:{group_id}', is_group=True)
        except Exception:
            log.exception('assess_scheduled_recording_impact failed for test %d', test_id)


@retry_on_locked()
def dismiss_recording_failing_alerts(recording_id: int):
    """Auto-dismiss any active RECORDING_CHANNEL_FAILING alert(s) for `recording_id` -
    called whenever a recording leaves SCHEDULED (started, cancelled, deleted): a
    warning about a recording that no longer exists (in that state) is noise
    (DESIGN-prerecord-checks.md §2). No-op if none exist. Whole-function retry is safe
    here: every retry re-queries fresh and the only side effect is this commit."""
    from . import db
    from .database import Alert
    from datetime import datetime

    prefix = f'recfail:rec:{recording_id}:'
    for a in Alert.query.filter(
            Alert.alert_type == 'RECORDING_CHANNEL_FAILING',
            Alert.source.like(f'{prefix}%'),
            Alert.dismissed_at.is_(None)).all():
        a.dismissed_at = datetime.utcnow()
    db.session.commit()
