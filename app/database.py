import json
from datetime import datetime
from . import db

# ── Event type constants ──────────────────────────────────────────────────────

SEGMENT_STARTED        = 'SEGMENT_STARTED'
SEGMENT_ENDED          = 'SEGMENT_ENDED'
STALL_DETECTED         = 'STALL_DETECTED'
RESTART_ATTEMPTED      = 'RESTART_ATTEMPTED'
RESTART_SUCCEEDED      = 'RESTART_SUCCEEDED'
RESTART_FAILED         = 'RESTART_FAILED'
# Fires when the capture phase ends (stop time / graceful stop) and concatenation is
# about to begin - NOT when the recording is fully done (that's CONCATENATION_DONE).
# Renamed from RECORDING_COMPLETE 2026-07-16; existing rows migrated in _migrate_db().
CAPTURE_COMPLETE       = 'CAPTURE_COMPLETE'
CONCATENATION_STARTED  = 'CONCATENATION_STARTED'
CONCATENATION_DONE     = 'CONCATENATION_DONE'
RECORDING_FAILED                    = 'RECORDING_FAILED'
RECORDING_FAILED_DEAD_STREAM        = 'RECORDING_FAILED_DEAD_STREAM'
# Dead-stream fast-fail tripped but the retry budget (watchdog.dead_stream_max_retry_attempts)
# and the recording's own window both still have room - status -> RETRYING instead of an
# immediate RECORDING_FAILED_DEAD_STREAM. See app/watchdog.py's dead-stream give-up branch and
# Product Principle 2 ("complete the recording at almost all costs").
RECORDING_RETRY_SCHEDULED           = 'RECORDING_RETRY_SCHEDULED'
RECORDING_RESUMED                   = 'RECORDING_RESUMED'
# resume_recording() found an "open" (ended_at=None) segment whose file was still growing
# on disk - a live process almost certainly already owns this recording. Refused to close
# the segment or launch a second capture; no state was changed. See dev/docs/BUGS.md
# 2026-08-14 and app/recorder.py::_segment_file_is_growing().
RECORDING_RESUME_REFUSED            = 'RECORDING_RESUME_REFUSED'
# The service was not running when this recording's stop time passed - proven, not
# guessed: had the app been up, the stop job would have fired and closed the recording
# normally. So capture ended when the app died and the rest of the scheduled window was
# never recorded. Distinct from RECORDING_RESUMED, which is the routine "picked this back
# up after a restart" marker and reads as normal operation on its own. See
# app/scheduler.py::_report_capture_lost_to_outage() and dev/docs/BUGS.md 2026-08-17.
CAPTURE_LOST_TO_OUTAGE              = 'CAPTURE_LOST_TO_OUTAGE'
# start_recording() deferred (recording.post_process.collision_policy == 'wait') because a
# live mp4 conversion was using local resources when this recording's start_time arrived. It
# retries on a short timer and either starts once the conversion clears, or is marked
# RECORDING_FAILED if its own stop_time passes first. See app/recorder.py::start_recording()
# and app/scheduler.py::reschedule_recording_start().
RECORDING_START_DEFERRED            = 'RECORDING_START_DEFERRED'
RECORDING_ABORTED                   = 'RECORDING_ABORTED'
RECORDING_PAUSED                    = 'RECORDING_PAUSED'
RECORDING_MANUALLY_STOPPED          = 'RECORDING_MANUALLY_STOPPED'
RECORDING_CREATED_AFTER_EVENT_START = 'RECORDING_CREATED_AFTER_EVENT_START'
RECORDING_STARTED_LATE              = 'RECORDING_STARTED_LATE'
RECORDING_STOPPED_EARLY             = 'RECORDING_STOPPED_EARLY'
RECORDING_EDITED                    = 'RECORDING_EDITED'
RECORDING_STOP_TIME_ADJUSTED        = 'RECORDING_STOP_TIME_ADJUSTED'
RECORDING_HANDOFF                   = 'RECORDING_HANDOFF'
# Group-backed recordings (Recording.group_id set): which member feed was chosen at record
# start, and mid-recording switches to another member after the active feed died.
GROUP_MEMBER_SELECTED               = 'GROUP_MEMBER_SELECTED'
GROUP_FAILOVER                      = 'GROUP_FAILOVER'
# The group's format lock left no eligible member and the recording went ahead anyway,
# from the best-ranked recording-enabled member whatever its format
# (DESIGN-channel-groups-model.md 15.2). The third of that section's three voices, and the
# only one that survives onto the artifact: without it there is a file six weeks later
# whose format nobody can explain.
RECORDING_FORMAT_OVERRIDE           = 'RECORDING_FORMAT_OVERRIDE'
# A segment was captured at a different resolution/fps than the one this recording opened
# with, so the finished file changes format part-way through (DECIDED 12,
# DESIGN-channel-groups-model.md 5.1). Written off the segment's own capture-time probe
# rather than off the failover decision, because the same member's feed can drift under us
# without any failover at all - measured, not assumed: the concat and the mp4 conversion
# both absorb the change without an error, and the mp4 then declares one resolution for a
# file that has two (dev/changelog/754). Silence here is exactly the defect this app exists
# to refuse, so the fact rides on the artifact.
RECORDING_FORMAT_CHANGED            = 'RECORDING_FORMAT_CHANGED'
# Channel-backed (non-group) recording whose frozen Recording.url no longer matched the
# channel's current stream_url - the provider rewrote its stream domain and/or embedded
# creds and sync repointed the Channel row. Logged only when the URL actually changed.
RECORDING_URL_RERESOLVED            = 'RECORDING_URL_RERESOLVED'
# Logged on the NEW recording when it was created via "Find Another Airing" replace mode
# (the SCHEDULED recording it replaced is deleted in the same request).
RECORDING_REPLACED_OTHER            = 'RECORDING_REPLACED_OTHER'
# A SCHEDULED recording's channel_id/url were rewritten by the duplicate-repoint helper
# (DESIGN-sync-resilience.md §6) - the channel it targeted was missing from the provider
# feed and the same stream survives on another channel row.
RECORDING_REPOINTED                 = 'RECORDING_REPOINTED'
# Timeline damage found in the concatenated .ts (gaps/missing frames that break player
# seeking) - logged when post_process.reencode_mode='damaged' decides to re-encode.
SEEK_DAMAGE_DETECTED   = 'SEEK_DAMAGE_DETECTED'
# The capture's frame rate changed partway through (a stall restart landing on a different
# variant of the same feed) and post_process.reencode_mode='damaged' chose to re-encode to
# normalize it to one constant rate. Deliberately NOT SEEK_DAMAGE_DETECTED: the timeline is
# intact, and reporting a rate change as missing video is the exact defect this pair of
# events replaced (dev/changelog/866). One flag, one meaning.
MIXED_FRAME_RATE_DETECTED = 'MIXED_FRAME_RATE_DETECTED'
# A captured segment was thrown away instead of being joined into the final file, because
# what came back was the provider's "channel offline" placeholder clip rather than the
# channel. Detected by ratio, never by byte count: a finite clip drained at 250-500x real
# time delivers ten minutes of content in five seconds of wall clock and then ends on a
# clean EOF, which no live feed does (dev/changelog/957). An action the app took, so it is
# its own type rather than a DIAGNOSTICS measurement - and a loud one, because the
# alternative is 60 minutes of black in the middle of a race that nothing on any surface
# explains.
SEGMENT_DISCARDED      = 'SEGMENT_DISCARDED'
# A capture was killed because its feed kept delivering content faster than real time for a
# sustained window - the signature of a provider re-serving the same few seconds over and
# over (dev/changelog/964). Named for what was MEASURED and not for what it is believed to
# mean: the ratio proves the delivery rate, it does not prove the picture is frozen, and an
# event asserting a diagnosis this app cannot make would be the unexplainable number
# Product Principle 1 forbids, pointed the other way. Its own type rather than a
# DIAGNOSTICS measurement for the same reason SEGMENT_DISCARDED is - the app acted.
FAST_DELIVERY_DETECTED = 'FAST_DELIVERY_DETECTED'
# Generic carrier for "here is something the app measured", emitted on both good and bad
# verdicts so a healthy recording still says what was checked (dev/changelog/330). The
# specific measurement is named in extra_data['kind'] ('timeline_scan', 'capture_health',
# ...), NOT by adding a new event type per stat - a future diagnostic costs no schema, no
# ev_cls entry and no catalog churn. Never overload SEEK_DAMAGE_DETECTED for this: that
# name asserts damage, and one flag must mean one thing.
DIAGNOSTICS            = 'DIAGNOSTICS'
# The concat's output is committed and post-processing has started reading it back
# (REC_STATUS_ANALYZING). Distinct from CONCATENATION_DONE, which is the concat's own
# closing fact: this one says the NEXT phase has begun, and it is what stops the event log
# reading as though a finished concat had restarted (dev/changelog/867).
POSTCAPTURE_ANALYSIS_STARTED = 'POSTCAPTURE_ANALYSIS_STARTED'
# The same phase was reached again - a service restart, a crash resume, a Retry - and skipped
# because Recording.analysis_completed_at says it already finished. Its own type rather than a
# differently-worded POSTCAPTURE_ANALYSIS_STARTED: one flag, one meaning, and the timeline has
# to be able to say "this file was not re-read" rather than implying it was
# (dev/changelog/951).
POSTCAPTURE_ANALYSIS_SKIPPED = 'POSTCAPTURE_ANALYSIS_SKIPPED'
CONVERSION_STARTED     = 'CONVERSION_STARTED'
# A supervised conversion attempt died or stalled and auto-restart re-spawned ffmpeg
# (post_process.auto_restart) - a stream copy from scratch, a re-encode from its
# conversion_parts_* checkpoint (dev/changelog/955). Distinct from CONVERSION_STARTED so the
# event log reads honestly when a conversion loops - one flag, one meaning.
CONVERSION_RESTARTED   = 'CONVERSION_RESTARTED'
# A conversion yielded local resources to a recording (recording.post_process.
# collision_policy) - either parked before it started, or suspended mid-encode, because a
# recording is IN_PROGRESS, imminent, or doing its own post-capture work. Distinct from
# CONVERSION_RESTARTED: this is never a failure and never counts against
# max_restart_attempts.
CONVERSION_YIELDED     = 'CONVERSION_YIELDED'
# The wait a CONVERSION_YIELDED opened is over and the conversion is working again. Its own
# type rather than a second CONVERSION_YIELDED with different wording: a yield with no
# resume after it means the app is still waiting, and that has to be readable off the
# timeline rather than inferred from what follows (dev/changelog/952).
CONVERSION_RESUMED     = 'CONVERSION_RESUMED'
CONVERSION_DONE        = 'CONVERSION_DONE'
FILE_MOVED             = 'FILE_MOVED'
SCRIPT_EXECUTED        = 'SCRIPT_EXECUTED'
# Pre-recording health check (DESIGN-prerecord-checks.md §3-4): a channel_tester run fired
# a lead time before this recording's start_time, testing the channel (or would-be group
# member) that will actually record. Logged on the recording being protected.
PRE_CHECK_PASSED       = 'PRE_CHECK_PASSED'
PRE_CHECK_FAILED       = 'PRE_CHECK_FAILED'
PRE_CHECK_SKIPPED      = 'PRE_CHECK_SKIPPED'

# ── Channel event type constants ──────────────────────────────────────────────

CHANNEL_ADDED_TO_GUIDE          = 'CHANNEL_ADDED_TO_GUIDE'
CHANNEL_REMOVED_FROM_GUIDE      = 'CHANNEL_REMOVED_FROM_GUIDE'
CHANNEL_HEALTH_OVERRIDE_CHANGED = 'CHANNEL_HEALTH_OVERRIDE_CHANGED'
CHANNEL_GROUPED                 = 'CHANNEL_GROUPED'
CHANNEL_UNGROUPED               = 'CHANNEL_UNGROUPED'
# Written by apply_failover_health_observation (app/health_score.py) on the *abandoned*
# member when a group-backed recording fails over mid-flight - the score hit that member
# takes is otherwise invisible on its own Activity Timeline (dev/docs/BUGS.md 2026-08-10).
CHANNEL_FAILOVER_HEALTH_OBSERVATION = 'CHANNEL_FAILOVER_HEALTH_OBSERVATION'
# The sibling of the above for a member a recording moved off VOLUNTARILY because it kept
# stalling (dev/changelog/889). A separate type because the two carry different scores and
# mean different things: that one is the recording fail floor on a feed that died, this one
# is the member's own measured share on a feed that was still delivering.
CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION = 'CHANNEL_STALL_DEMOTION_HEALTH_OBSERVATION'
# The third of that family, for a member that answered a recording with the provider's
# "channel offline" placeholder clip instead of the channel (app/health_score.py::
# apply_placeholder_health_observation, dev/changelog/957). Its own type for the same reason
# the two above are separate: this one carries the flat recording fail floor on a feed that
# demonstrably served no content, which is a different fact from either a feed that died or
# a feed that was still delivering while stalling.
CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION = 'CHANNEL_PLACEHOLDER_HEALTH_OBSERVATION'
# The fourth, for a member whose feed delivered content faster than real time for a
# sustained window during a recording (app/health_score.py::
# apply_fast_delivery_health_observation, dev/changelog/964). Separate from the placeholder
# type beside it although both carry the same flat fail floor: one member served a finite
# offline clip and the other served a live-looking stream that was worthless, and a channel
# page that could not tell those apart would send its reader looking for the wrong problem.
CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION = 'CHANNEL_FAST_DELIVERY_HEALTH_OBSERVATION'
# Written by _write_channel_url_drift_events (app/accounts.py) for every channel a sync
# rewrote Channel.stream_url on (DESIGN-url-drift.md 4/3) - the per-channel counterpart to
# the account-level PROVIDER_URLS_CHANGED alert, which only fires above a channel-count
# threshold. Written for every drifted channel regardless of that threshold, by design.
CHANNEL_URL_CHANGED = 'CHANNEL_URL_CHANGED'
# The human's hide/show answer for this channel moved - Channel.hidden_override, written
# only by channel_hiding.set_hidden_override(). Deliberately the ONLY hide-related event:
# Channel.hidden is a derived cache recomputed in bulk over the whole table, so logging its
# effective moves would write tens of thousands of events saying nothing a rule change does
# not already explain (dev/changelog/775).
CHANNEL_HIDE_OVERRIDE_CHANGED = 'CHANNEL_HIDE_OVERRIDE_CHANGED'
# The user rolled this channel's health score back by hand - a full reset, or one
# step-back-one-observation click (app/health_recompute.py, dev/changelog/895). ONE type for
# both: they are the same mechanism at two sizes, and extra_data['action'] says which. The
# score moving without an observation behind it is exactly the number a user cannot
# otherwise explain, so this event carries what was unwound and the before/after value.
CHANNEL_HEALTH_ROLLBACK = 'CHANNEL_HEALTH_ROLLBACK'
# The stored score was rewritten from this channel's observation ledger with nothing newly
# excluded, because the stored number had stopped matching what the ledger replays to
# (app/health_recompute.py::recompute_in_place). Deliberately NOT CHANNEL_HEALTH_ROLLBACK:
# that type asserts the user unwound an observation by hand, and nothing was unwound here.
# The score moving with no observation behind it is the number a user cannot otherwise
# explain, so this event carries the before/after value and the reason (dev/changelog/951).
CHANNEL_HEALTH_RECOMPUTED = 'CHANNEL_HEALTH_RECOMPUTED'

# ── Channel group event type constants ────────────────────────────────────────
#
# ChannelGroupEvent carries facts about a GROUP that ChannelEvent structurally cannot:
# channel_events is keyed on channel_id with no group_id, so it cannot answer a
# per-membership question, and channels here belong to more than one group. A
# channel-scoped fact still belongs on ChannelEvent - member add/remove stays
# CHANNEL_GROUPED/CHANNEL_UNGROUPED and must not be duplicated here.

# A participation checkbox changed on one membership: which one, which direction, and
# who changed it. Always carries channel_id.
GROUP_MEMBER_PARTICIPATION      = 'GROUP_MEMBER_PARTICIPATION'
# One member's known video format was found to differ from (mismatch) / to have returned
# to (resolved) THIS group's effective format reference. Written by the reconcile engine
# (app/channel_groups.py::_log_and_alert_reconcile), which also uses this log as its own
# durable state for "was this member mismatched last pass". Always carries channel_id.
#
# Per-membership, not per-channel, and that is load bearing rather than tidy: the same
# member is an outlier in one group and conforming in another, so while these lived on
# ChannelEvent (no group_id) two groups shared one state slot and took turns overwriting
# it - logging MISMATCH then RESOLVED on every pass, forever, and never dismissing the
# alert (dev/changelog/789). Pairs with the GROUP_FORMAT_MISMATCH alert, whose source key
# was already group-scoped.
CHANNEL_GROUP_FORMAT_MISMATCH   = 'CHANNEL_GROUP_FORMAT_MISMATCH'
CHANNEL_GROUP_FORMAT_RESOLVED   = 'CHANNEL_GROUP_FORMAT_RESOLVED'
# The group's format strategy moved the format lock: the old format, the new one, the
# strategy that chose it, and the numbers behind it. Group-wide, so channel_id is NULL.
GROUP_FORMAT_STRATEGY_APPLIED   = 'GROUP_FORMAT_STRATEGY_APPLIED'
# The standing strategy ran and no format won - no bucket has enough healthy members to
# build on, which is every group's state until its members have been tested. A distinct
# type from APPLIED rather than an outcome flag on it: nothing was applied, and one type
# meaning two things is what the states-are-enumerated rule refuses. Written once, on the
# way into the state, never on every run (dev/changelog/753).
GROUP_FORMAT_STRATEGY_BLOCKED   = 'GROUP_FORMAT_STRATEGY_BLOCKED'
# What a health check is going to test changed for a reason that was not a membership
# edit - today only the one-time retarget of the pinned TV Guide check (dev/changelog/752).
# Carries the old and new probe counts. Group-wide, so channel_id is NULL.
GROUP_CHECK_TARGETS_CHANGED     = 'GROUP_CHECK_TARGETS_CHANGED'
# A warning banner was hidden or re-armed for this group by hand. Carries the banner kind
# and the direction. Group-wide, so channel_id is NULL. Hiding a warning is a judgment
# call a human made about their own setup, so it leaves a trace like every other one
# (DESIGN-channel-groups-model.md 16.2, dev/changelog/756).
GROUP_WARNING_MUTED             = 'GROUP_WARNING_MUTED'
# The group joined the TV Guide: by hand, or as the last step of the promotion
# walkthrough (DESIGN-channel-groups-model.md 14.1). Group-wide, so channel_id is NULL.
GROUP_GUIDE_ADDED               = 'GROUP_GUIDE_ADDED'
# The group left the TV Guide, with `detail` naming why - by hand, or because its last
# recording-enabled member was switched off or removed and the user confirmed it
# (DESIGN-channel-groups-model.md 15, breach paths 2 and 3), in which case it also carries
# how many scheduled recordings were cancelled with it. One type for one visible state
# change with the cause in the detail, the same shape GROUP_MEMBER_PARTICIPATION uses for
# a switch moving in either direction; the DIRECTION is what earns its own type above,
# because a row appearing and a row vanishing are two states, not one (dev/changelog/764).
# Group-wide, so channel_id is NULL - the row is about the group's guide status, not about
# whichever membership happened to be last, and that membership already logged its own move.
GROUP_GUIDE_REMOVED             = 'GROUP_GUIDE_REMOVED'
# The group is in the TV Guide with nothing switched on for recording, reached down a
# path with no human present to confirm it (a bulk delete of channels the provider
# dropped). The group deliberately STAYS in the guide - pulling a row overnight is the
# silent behavior 15 refuses - so this records the broken state for the Activity Timeline
# while the alert and the group page's banner carry it to the user. Group-wide.
GROUP_GUIDE_BROKEN              = 'GROUP_GUIDE_BROKEN'

# ── Models ────────────────────────────────────────────────────────────────────
#
# A column that some migration adds with a SQL DEFAULT must also declare an equal
# server_default here, or the two vintages of this schema disagree: create_all() emits no
# DEFAULT for a Python-side default=, so the column carries one on every upgraded database
# and none on every fresh one. The ORM hides that (it always supplies a value), but a raw
# INSERT that omits the column then succeeds on one vintage and dies on NOT NULL on the
# other - which aborts startup when the writer is a migration step (dev/changelog/687, and
# dev/changelog/690 for the sweep that closed the remaining 22). server_default takes
# db.text() rather than a bare string so the rendered DDL is `DEFAULT 0`, matching what the
# migration wrote, instead of `DEFAULT '0'`. Enforced by
# tests/test_static_invariants.py::MigrationServerDefaultParityTests.

class RecordingProfile(db.Model):
    """Named preset of recording settings, selectable per-recording or as a
    channel's default. Each overridable field is nullable: None = fall back to
    the corresponding global config.yaml value (same convention as
    Account.max_connections / Account.url_normalization)."""
    __tablename__ = 'recording_profiles'

    id                        = db.Column(db.Integer, primary_key=True)
    name                      = db.Column(db.String(255), nullable=False)
    # None = use global recording.filename_template
    filename_template         = db.Column(db.String(512))
    pre_padding_minutes       = db.Column(db.Integer, nullable=False, default=0,
                                          server_default=db.text('0'))
    post_padding_minutes      = db.Column(db.Integer, nullable=False, default=0,
                                          server_default=db.text('0'))
    # None = use global watchdog.stall_timeout_seconds / restart_delay_seconds / max_consecutive_failures
    stall_timeout_seconds     = db.Column(db.Integer)
    restart_delay_seconds     = db.Column(db.Integer)
    max_consecutive_failures  = db.Column(db.Integer)
    # None = use global watchdog.stall_move_count / stall_move_window_minutes. The
    # stall-rate demotion trigger (dev/changelog/889); 0 on the count disables it for
    # this profile even when a global trigger is set.
    stall_move_count          = db.Column(db.Integer)
    stall_move_window_minutes = db.Column(db.Integer)
    # None = use global recording.retention_days; 0 = never auto-delete (overrides a
    # nonzero global back to "keep forever" for this profile's recordings)
    retention_days            = db.Column(db.Integer)
    # None = use global channel_testing.pre_check.enabled; tri-state override
    pre_check_enabled         = db.Column(db.Boolean)
    created_at                = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at                = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HealthCheckProfile(db.Model):
    """Named preset of channel-test execution settings, selectable when creating
    an on-demand test job. Each field is nullable: None = fall back to the
    corresponding channel_testing.* config.yaml value (same convention as
    RecordingProfile)."""
    __tablename__ = 'health_check_profiles'

    id                             = db.Column(db.Integer, primary_key=True)
    name                           = db.Column(db.String(255), nullable=False)
    test_duration_seconds          = db.Column(db.Integer)
    wait_between_channels_seconds  = db.Column(db.Integer)
    screenshots_enabled            = db.Column(db.Boolean)   # tri-state: None/True/False
    connect_retries                = db.Column(db.Integer)
    connect_timeout_seconds        = db.Column(db.Integer)
    connect_retry_delay_seconds    = db.Column(db.Integer)
    created_at                     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at                     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Recording.status vocabulary. Named so status comparisons never re-type the string literal -
# see CLAUDE.md's "one flag, one meaning; states are enumerated" defect-class rule.
REC_STATUS_SCHEDULED     = 'SCHEDULED'
REC_STATUS_IN_PROGRESS   = 'IN_PROGRESS'
REC_STATUS_PAUSED        = 'PAUSED'
REC_STATUS_RETRYING      = 'RETRYING'
REC_STATUS_CONCATENATING = 'CONCATENATING'
# The post-capture analysis phase: the concat has committed its output and the app is
# reading that finished file back (health probe, timeline damage scan, near-empty segment
# detection). Whole-file reads that scale with size - ~124s each on a 17.9 GB capture - so
# this is not a moment, it is a window that reaches hours. It exists because CONCATENATING
# used to stand for it, and a row saying "concatenating" while the concat has been finished
# the whole time is the founding-thesis defect: an activity log that read "concat complete",
# then "resuming concatenation", then "concat failed" for a recording sitting whole on disk
# (dev/changelog/867).
REC_STATUS_ANALYZING     = 'ANALYZING'
REC_STATUS_CONVERTING    = 'CONVERTING'
REC_STATUS_COMPLETED     = 'COMPLETED'
REC_STATUS_FAILED        = 'FAILED'
REC_STATUS_ABORTED       = 'ABORTED'

# ChannelTest.status vocabulary. Shares string values with REC_STATUS_* above (and with
# OnDemandTestJob.status, which has its own separate QUEUED/RUNNING/... vocabulary and no
# constants of its own) - kept as distinct names because the two are different enumerations
# that happen to spell some members the same way.
TEST_STATUS_COMPLETED = 'COMPLETED'
TEST_STATUS_FAILED    = 'FAILED'
TEST_STATUS_CANCELLED = 'CANCELLED'

# Statuses that own a live ffmpeg child or an in-flight background thread, so restarting
# the app (or deleting the row) would strand work. SCHEDULED is deliberately absent - a
# scheduled recording is re-armed at startup by resume_in_progress_recordings(). RETRYING owns
# no live process (it's a scheduled retry_<id> job waiting to fire) - the persistent APScheduler
# jobstore would in principle survive a restart on its own, but blocking here is the safe
# default rather than betting the first version of dead-stream retry on that path.
RESTART_BLOCKING_STATUSES = (
    REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
)

# Statuses whose scheduled window is open: started, not finished, whether or not a capture
# is running inside it right now. PAUSED and RETRYING are reachable only from IN_PROGRESS,
# so no row in this set can carry a future start_time - a query that pairs these with
# SCHEDULED under a `start_time > now` clause matches them zero times, forever, without
# erroring (dev/changelog/663).
WINDOW_OPEN_STATUSES = (REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING)


class Recording(db.Model):
    __tablename__ = 'recordings'
    # AUTOINCREMENT, so a deleted recording's id is never handed to the next one. Without it
    # SQLite issues max(id)+1, which means deleting the newest recording and creating another
    # silently gives the new one the old one's number - and anything still holding that number
    # re-attaches to it rather than dangling. Two alerts did exactly that (dev/changelog/929).
    # detach_recording_references() stays regardless; the two are belt and braces, and only
    # this half protects a consumer nobody has written yet (dev/changelog/937).
    __table_args__ = {'sqlite_autoincrement': True}

    id                        = db.Column(db.Integer, primary_key=True)
    name                      = db.Column(db.String(255), nullable=False)
    url                       = db.Column(db.String(2048), nullable=False)
    start_time                = db.Column(db.DateTime, nullable=False)   # naive UTC
    stop_time                 = db.Column(db.DateTime, nullable=False)   # naive UTC
    output_path               = db.Column(db.String(1024))
    # SCHEDULED | IN_PROGRESS | PAUSED | RETRYING | CONCATENATING | ANALYZING | CONVERTING |
    # COMPLETED | FAILED | ABORTED - see REC_STATUS_* above
    status                    = db.Column(db.String(32), nullable=False, default=REC_STATUS_SCHEDULED)

    total_stall_count         = db.Column(db.Integer, default=0)
    total_restart_count       = db.Column(db.Integer, default=0)
    consecutive_failures      = db.Column(db.Integer, default=0)
    consecutive_failures_peak = db.Column(db.Integer, default=0)
    # Wall-clock seconds with no data being written: per stall, the window between the
    # last byte and the stall being declared, plus the restart delay and the reconnect
    # wait that follow it (app/watchdog.py). Rows written before 2026-08-01 carry the old
    # meaning - the sum of restart delays alone - and are not comparable
    # (dev/changelog/432). Recording.capture_gap_seconds is the post-capture twin, and is
    # a strict subset: it sees only the time no segment was running at all, never the
    # no-growth window inside a segment that had stopped delivering but was not yet killed.
    total_downtime_seconds    = db.Column(db.Float, default=0.0)
    final_file_size           = db.Column(db.Integer)
    # MAX_CONSECUTIVE_FAILURES | DEAD_STREAM_DETECTED | FAST_DELIVERY_DETECTED - set only when
    # status becomes FAILED, and only by app/watchdog.py::_mark_recording_failed
    failure_reason            = db.Column(db.String(64))
    # Dead-stream fast-fail retry budget (watchdog.dead_stream_max_retry_attempts). Counts
    # attempts scheduled, not just fired - incremented when status -> RETRYING, never reset
    # (a mid-window recovery that later dies again keeps counting against the same budget, so
    # a flapping stream cannot re-arm 10 fresh attempts per trip). next_retry_at is display-only
    # (the retry_<id> APScheduler job is the actual timer) and is cleared on leaving RETRYING.
    dead_stream_retry_count   = db.Column(db.Integer, nullable=False, default=0,
                                          server_default=db.text('0'))
    next_retry_at             = db.Column(db.DateTime)

    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_at           = db.Column(db.DateTime)
    completed_at         = db.Column(db.DateTime)
    # naive UTC; the window the app committed to capture. Preserved when the RUNTIME
    # adjusts start_time/stop_time (started late, stopped early, aborted) - that drift is
    # exactly what these record. Re-baselined by a user edit of a still-SCHEDULED
    # recording, which is a new plan rather than drift; the RECORDING_EDITED event holds
    # the previous times (dev/changelog/471).
    scheduled_start_time = db.Column(db.DateTime)
    scheduled_stop_time  = db.Column(db.DateTime)
    program_start_time   = db.Column(db.DateTime)   # naive UTC; immutable EPG program air-time snapshot at creation (None for manual recordings)
    program_stop_time    = db.Column(db.DateTime)   # naive UTC; immutable EPG program air-time snapshot at creation (None for manual recordings)
    program_title        = db.Column(db.String(512))  # immutable EPG program title snapshot at creation (None for manual recordings)
    program_sub_title    = db.Column(db.String(512))  # immutable EPG program sub-title snapshot at creation (None for manual recordings)

    # ── Channel link & health ─────────────────────────────────────────────────
    channel_id               = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=True)
    # Set when the recording was created from a channel-group guide row: channel_id/url
    # are re-resolved to the group's best member at record start and may be rewritten
    # mid-recording by group failover (app/watchdog.py).
    group_id                 = db.Column(db.Integer, db.ForeignKey('channel_groups.id'), nullable=True)
    # None = no profile; recording uses global config defaults throughout
    profile_id                = db.Column(db.Integer, db.ForeignKey('recording_profiles.id'), nullable=True)
    channel_health_snapshot  = db.Column(db.Text)           # JSON snapshot of ChannelTest at record-start
    recorded_resolution      = db.Column(db.String(32))     # ffprobe: e.g. "1920x1080"
    recorded_fps             = db.Column(db.Float)          # ffprobe: frames per second
    recorded_frame_count     = db.Column(db.Integer)        # ffprobe: nb_read_packets
    recorded_duration_seconds = db.Column(db.Float)         # ffprobe: format duration
    recorded_frame_pct       = db.Column(db.Float)          # actual_frames / (fps * scheduled_secs) * 100
    recorded_bitrate_kbps    = db.Column(db.Float)          # ffprobe: format bit_rate / 1000
    recorded_audio_codec     = db.Column(db.String(64))
    recorded_audio_channels  = db.Column(db.Integer)
    recorded_audio_sample_rate  = db.Column(db.Integer)     # ffprobe: Hz
    recorded_audio_bitrate_kbps = db.Column(db.Float)
    recorded_audio_language     = db.Column(db.String(32))  # stream tag, e.g. "eng"

    # ── Seek-scan timeline stats (app/probe.py::scan_video_timeline) ──────────
    # Measured on the concatenated .ts during post-process; names deliberately mirror
    # ChannelTest.timeline_* so both sides of the same scanner match and can be joined
    # without a translation layer (dev/changelog/330). Gaps are measured on the DECODE
    # timeline (dts_time), never PTS - B-frame reordering makes PTS useless here.
    # span_seconds / packet_count / fps are deliberately NOT columns: they duplicate
    # recorded_duration_seconds / recorded_frame_count / recorded_fps on this same row.
    timeline_gap_count       = db.Column(db.Integer)
    timeline_gap_seconds     = db.Column(db.Float)
    timeline_max_gap_seconds = db.Column(db.Float)
    timeline_deficit_seconds = db.Column(db.Float)
    # The assess_seek_damage() verdict AS EVALUATED AT SCAN TIME, against the thresholds in
    # force then. If DAMAGE_MIN_MISSING_SECONDS / DAMAGE_MIN_MISSING_FRACTION are ever
    # retuned, stored verdicts will not agree with a fresh evaluation. That is correct for
    # an audit trail - this column records what the app decided and acted on - but it must
    # never be treated as recomputable.
    timeline_damaged         = db.Column(db.Boolean)

    # ── Near-empty/slate segment detection (app/postprocessor.py::_detect_near_empty_segments) ──
    # A segment can be timeline-clean (frames present, evenly spaced) and still be a
    # blank/slate screen - bytes-per-second far below the recording's own average, per
    # segment. Rollup of RecordingSegment.near_empty below.
    near_empty_segment_count = db.Column(db.Integer)
    near_empty_seconds       = db.Column(db.Float)

    # ── Discarded segments (app/watchdog.py, rolled up by app/concatenator.py) ──
    # How many segments were kept out of the final file and how many seconds of content they
    # held between them - the provider's placeholder clip, today. Columns rather than a scan
    # of RecordingSegment.excluded_reason because both are displayed on the detail page and
    # the count belongs in a list row's tooltip, and CLAUDE.md promotes a stat to a column
    # the moment it would be sorted, filtered or displayed. NULL means the recording never
    # reached the rollup (still capturing, or captured before this existed); 0 means it did
    # and discarded nothing, which is a real answer and not the same thing.
    discarded_segment_count  = db.Column(db.Integer)
    discarded_seconds        = db.Column(db.Float)

    # ── Segments whose video arrived faster than the clock (app/concatenator.py) ──
    # How many joined segments hold more content than the seconds they ran for, by more than
    # watchdog.fast_delivery_surplus_seconds, and how much content those segments put into
    # the finished file. Kept, not discarded: under the live thresholds the content is
    # unexplained rather than proven bad, so the recording is flagged and the file is left
    # intact for a human to judge (dev/changelog/966).
    #
    # The seconds are the reason this is not just a count: they answer "how much of what I am
    # about to watch is suspect", which is the question a flag on a week-old recording exists
    # to answer. Same NULL/0 split as the discard rollup above.
    fast_delivery_segment_count = db.Column(db.Integer)
    fast_delivery_seconds       = db.Column(db.Float)

    # ── Stream format profile of the FINAL/CONVERTED output file ──────────────
    # Mirrors the ChannelTest quality-profile columns (DESIGN-stream-quality-profile.md)
    # but takes this model's recorded_ prefix, which is what keeps "of the output file"
    # honest: a re-encode changes codec and pixel format, so this is not necessarily what
    # the provider sent. RecordingSegment.probe_* holds the ORIGINAL capture; routes read
    # the segment first and label a recorded_* fallback 'output' (dev/changelog/330).
    # Informational only - NEVER blend any of this into health_score or failover ranking.
    recorded_video_codec          = db.Column(db.String(32))  # ffprobe codec_name, e.g. "h264", "hevc"
    recorded_pix_fmt              = db.Column(db.String(32))  # raw ffprobe value, e.g. "yuv420p10le"
    recorded_bit_depth            = db.Column(db.Integer)     # parsed from pix_fmt (8/10/12)
    recorded_chroma_subsampling   = db.Column(db.String(8))   # "420" | "422" | "444", parsed from pix_fmt
    recorded_interlaced           = db.Column(db.Boolean)     # field_order != progressive; NULL = unknown
    recorded_coded_resolution     = db.Column(db.String(32))  # coded_width x coded_height, only when != resolution
    recorded_is_vfr               = db.Column(db.Boolean)     # r_frame_rate vs avg_frame_rate mismatch; NULL = undetermined
    recorded_bits_per_pixel_frame = db.Column(db.Float)       # bitrate_bps / (w*h*fps) - efficiency stat, not a verdict
    health_gathered_at       = db.Column(db.DateTime)       # when ffprobe ran
    health_quality_score     = db.Column(db.Integer)        # this recording's own 0-100 health-score contribution
    health_quality_breakdown = db.Column(db.Text)           # JSON: per-penalty math behind health_quality_score
    health_blend_breakdown   = db.Column(db.Text)           # JSON: decay/blend math for this observation
    capture_quality_breakdown = db.Column(db.Text)          # JSON: per-penalty math behind the postprocess damage/near-empty correction (app/health_score.py::apply_capture_quality_correction)

    # The post-capture analysis phase finished for this recording. A RECORDED fact, and the
    # only thing do_postprocess() reads to decide whether to run that phase again - status is
    # not that fact and must never be read as one, exactly as concatenator.py::
    # committed_concat_output() says of the concat one phase earlier. Written in the same
    # commit as the phase's last and only non-idempotent step, the capture-quality blend, so
    # no crash can leave the blend applied with the phase still looking unfinished: a resume
    # that inferred "not analyzed yet" re-probed the whole file and blended a SECOND
    # observation into the channel's health score every time the service restarted, leaving a
    # stored score observation_ledger() could not reproduce (dev/changelog/951).
    #
    # Never cleared, including by a fresh concat: a second concat is only reachable while the
    # segments still exist, which means the first one never committed an output, which means
    # this phase never ran.
    analysis_completed_at    = db.Column(db.DateTime)

    # Supervised-conversion monitor state (app/postprocessor.py). All nullable; see
    # migration _m016. conversion_attempts counts every death (crash/stall/restart-kill)
    # and is reset to 0 by a manual Retry. The progress snapshot lets the detail strip and
    # dashboard render live progress from the row with no extra plumbing.
    # restarts so far this recording
    conversion_attempts      = db.Column(db.Integer, default=0, server_default=db.text('0'))
    conversion_progress_pct  = db.Column(db.Float)          # last computed 0-100 (null when duration unknown)
    conversion_out_size      = db.Column(db.Integer)        # last observed output file bytes
    conversion_eta_seconds   = db.Column(db.Integer)        # last smoothed ETA in seconds
    conversion_started_at    = db.Column(db.DateTime)       # wall-clock start of the current attempt
    conversion_updated_at    = db.Column(db.DateTime)       # when the progress snapshot above was written

    # When this recording's post-processing parked itself to let another recording have the
    # local CPU and disk (recording.post_process.collision_policy), NULL whenever it is
    # working. One meaning: "doing no work right now, waiting on another recording, and it
    # will pick itself back up". Both yield sites write it - the one before conversion has
    # started, where the chain simply polls and no ffmpeg exists, and the one mid-conversion,
    # where the ffmpeg is SIGSTOPped and continued (dev/changelog/952).
    #
    # It is a ROW rather than in-memory state because the restart guard is what needs to read
    # it: tools/check_busy.py is a separate CLI process with no view into this one, and a
    # parked recording must not refuse a restart it would cost nothing to allow - a process
    # that is paused is not, technically, running. Cleared on every exit from the wait, and
    # by the startup sweep, so a row can never be left claiming to be waiting for something
    # that is long over.
    #
    # It has a second reader for the same reason (dev/changelog/953): the collision check
    # counts a CONCATENATING or ANALYZING recording as a conflict, and a parked row holds
    # both of those statuses while consuming nothing. Two parked rows that each counted the
    # other would wait on each other forever, which is precisely the state recordings 17 and
    # 19 were in on 2026-09-13.
    postprocess_waiting_since = db.Column(db.DateTime)

    # WHO the park above is waiting on, snapshotted when it parked: the blocking recording's
    # name, and what that recording was doing in the words _conflict_phrase() already uses
    # ('is in progress', 'is joining its segments', ...). The three columns are ONE fact and
    # move together through set_postprocess_wait() (postprocessor.py) - never assign any of
    # them directly.
    #
    # They exist as columns rather than being read back out of the CONVERSION_YIELDED event
    # because the recordings list renders them per row, and parsing prose out of the event
    # log to display a list is the row-scaling defect CLAUDE.md's "promote a stat to a column
    # when you would display it" rule names. Snapshots rather than a foreign key for the same
    # reason: the list page would otherwise load the blocking row per parked row, and the
    # answer wanted is what was true when this recording stepped aside (dev/changelog/954).
    postprocess_waiting_on_name  = db.Column(db.String(500))
    postprocess_waiting_on_state = db.Column(db.String(200))

    # A re-encode's checkpoint: how much of the source is already encoded into part files on
    # disk, so a killed attempt costs one stretch of encoding instead of the whole job
    # (dev/changelog/955). The four columns are ONE fact and move together through
    # postprocessor.set_conversion_parts() - never assign any of them directly.
    #
    # They are a RECORDED fact, never inferred from the part files themselves: a part on disk
    # that no commit describes is verified from scratch (re-muxed and probed) before it is
    # adopted, and a crash between ffmpeg writing a part and this being committed degrades to
    # today's behavior - re-encode that stretch - rather than to a wrong splice point
    # (CLAUDE.md "Already done is a fact you recorded").
    #
    #  _parts_done      how many finished parts exist; their paths are DERIVED from the output
    #                   stem and the index, so nothing here can disagree with the filesystem.
    #  _source_covered  how far into the source those parts reach, measured from the last
    #                   decodable frame of each - never a container's declared duration, which
    #                   silently overshot by exactly 1.0s when it was measured.
    #  _source_complete the encode reached the end of the source. Its own fact rather than
    #                   "covered >= duration", so a restart during the final join redoes only
    #                   the join and never re-encodes a sliver off the end.
    #  _signature       a fingerprint of the encode-relevant ffmpeg arguments. Parts made under
    #                   different settings must never be joined: the audio-copy fallback
    #                   changes the audio codec config mid-file, and a changed crf leaves a
    #                   quality seam no one can see. A mismatch discards the parts and says so.
    conversion_parts_done             = db.Column(db.Integer, default=0, server_default=db.text('0'))
    conversion_source_covered_seconds = db.Column(db.Float)
    conversion_source_complete        = db.Column(db.Boolean, default=False,
                                                  server_default=db.text('0'))
    conversion_parts_signature        = db.Column(db.String(64))

    channel  = db.relationship('Channel', foreign_keys=[channel_id], lazy='joined')
    group    = db.relationship('ChannelGroup', foreign_keys=[group_id], lazy='joined')
    profile  = db.relationship('RecordingProfile', foreign_keys=[profile_id], lazy='joined')
    events   = db.relationship(
        'RecordingEvent', backref='recording', lazy=True,
        order_by='RecordingEvent.timestamp',
        cascade='all, delete-orphan',
    )
    segments = db.relationship(
        'RecordingSegment', backref='recording', lazy=True,
        order_by='RecordingSegment.segment_number',
        cascade='all, delete-orphan',
    )

    @property
    def duration_seconds(self):
        return (self.stop_time - self.start_time).total_seconds()

    @property
    def scheduled_duration_seconds(self):
        s = self.scheduled_start_time or self.start_time
        e = self.scheduled_stop_time  or self.stop_time
        return (e - s).total_seconds()

    @property
    def program_duration_seconds(self):
        if not (self.program_start_time and self.program_stop_time):
            return None
        return (self.program_stop_time - self.program_start_time).total_seconds()

    @property
    def captured_duration_seconds(self):
        """How much was actually captured, summed from data-bearing segments.

        Wall-clock span (ended_at - started_at) of every segment that wrote data,
        so it's I/O-free and matches how the detail-page timeline measures capture.
        It's a slight over-count of true content length (a stalled segment includes
        the up-to-stall_timeout tail before the kill), so it's used only as the
        'actual length' signal when there is no ffprobed final file - i.e. for
        FAILED/ABORTED recordings, which never concatenate. None if nothing captured.

        A discarded segment contributes nothing: it is not in the file this number is
        standing in for, so counting its span would claim length the artifact does not have.
        """
        total = 0.0
        for s in self.segments:
            if s.excluded:
                continue
            if s.bytes_recorded and s.started_at and s.ended_at:
                total += (s.ended_at - s.started_at).total_seconds()
        return total or None

    @property
    def actual_duration_seconds(self):
        """The real recorded length to show once a recording is terminal, else None.

        Prefers recorded_duration_seconds (ffprobe of the concatenated final file -
        the gold standard). FAILED/ABORTED never produce that file, so they fall back
        to the captured-segment span. Never the scheduled window (duration_seconds),
        which is what the UI used to conflate this with.
        """
        if self.recorded_duration_seconds:
            return self.recorded_duration_seconds
        if self.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED):
            return self.captured_duration_seconds
        return None

    @property
    def covered_capture_seconds(self):
        """Wall-clock seconds inside the recording window during which some segment was
        actually capturing - the union of the segment spans, clipped to the window.

        A union rather than a sum: overlapping spans would otherwise be counted twice and
        push the covered time past the window itself. Clipped to the window because a
        segment can end a second or two after stop_time (stop_time is stamped when the app
        decides to stop, ffmpeg finishes writing just after), and time outside the window
        is not time the window was covered.

        The adjusted window (stop_time - start_time) is the basis throughout, not the
        scheduled one: both ends are rewritten to the real times on an abort or an early
        manual stop, so this is the window the app was actually trying to fill. Same basis
        postprocessor._gather_recording_health uses for recorded_frame_pct.

        A discarded segment covers nothing, so its wall clock falls into capture_gap_seconds
        instead. That is the point rather than a side effect: a placeholder segment used to
        "cover" its five seconds like any other, which is why recording 19's 61s capture gap
        read as honest while twelve placeholder windows sat inside it (dev/changelog/957).
        """
        start, stop = self.start_time, self.stop_time
        if not start or not stop or stop <= start:
            return 0.0
        spans = []
        for s in self.segments:
            if s.excluded:
                continue
            if not s.started_at or not s.ended_at:
                continue
            a = max(s.started_at, start)
            b = min(s.ended_at, stop)
            if b > a:
                spans.append((a, b))
        spans.sort()
        covered = 0.0
        cur_a = cur_b = None
        for a, b in spans:
            if cur_b is not None and a <= cur_b:
                cur_b = max(cur_b, b)
            else:
                if cur_b is not None:
                    covered += (cur_b - cur_a).total_seconds()
                cur_a, cur_b = a, b
        if cur_b is not None:
            covered += (cur_b - cur_a).total_seconds()
        return covered

    @property
    def capture_gap_seconds(self):
        """Wall-clock seconds inside the recording window when NO segment was capturing at
        all, or None until the recording is over. A fact about the clock, not a claim about
        content: what the feed would have carried during a gap is unknowable from here.

        Distinct from total_downtime_seconds, which is a superset - downtime also counts
        the no-growth window inside a segment that had stopped delivering but had not yet
        been killed, which this cannot see because that segment was still running.

        Deliberately NOT a column, like the two properties around it - every input already
        has one, and deriving it keeps a single source of truth (CLAUDE.md §Measurements)
        while staying correct for every recording already in the database.
        """
        if self.actual_duration_seconds is None:
            return None
        return max(0.0, self.duration_seconds - self.covered_capture_seconds)

    @property
    def content_vs_capture_seconds(self):
        """Signed: how much more (+) or less (-) content came back than the wall clock the
        capture actually ran for. None until the recording is over.

        Positive means content arrived faster than real time. The measured cause on this
        app's providers is a buffer replayed at reconnect - every one of recording 14's 28
        segments delivered more content than its own wall clock, by a roughly constant
        13-29s regardless of whether the segment ran 16 seconds or 27 minutes, which is the
        signature of a fixed back-buffer prepended at each connect (dev/changelog/942).
        Negative means the feed stayed connected and delivered less than real time.

        Deliberately NOT called overlap or duplication. That the surplus is duplicate video
        was proven for one recording with framemd5 and is very often true, but nothing here
        compares a single frame, so this property must not assert it. Whether the replay
        happens to cover what a gap lost is likewise unknowable without frame-level
        matching - which is why capture_gap_seconds and this are reported side by side and
        never netted against each other. Netting them is the defect this replaced: the old
        content_shortfall_seconds was max(0, window - content), so a surplus silently
        cancelled real gap time and recording 14 reported "missing 0s (0%)" against 135.7s
        of measured gaps.
        """
        actual = self.actual_duration_seconds
        if actual is None:
            return None
        return actual - self.covered_capture_seconds

    @property
    def actual_duration_source(self):
        """Where actual_duration_seconds came from: 'file' (ffprobe of the final
        file) | 'segments' (captured-segment span) | None. Lets the UI label the
        number honestly instead of implying it's an ffprobed file length."""
        if self.recorded_duration_seconds:
            return 'file'
        if self.status in (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED) and self.captured_duration_seconds:
            return 'segments'
        return None

    @property
    def active_segment(self):
        return next(
            (s for s in reversed(self.segments) if s.ended_at is None),
            None
        )


class RecordingEvent(db.Model):
    __tablename__ = 'recording_events'

    id             = db.Column(db.Integer, primary_key=True)
    recording_id   = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    timestamp      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    event_type     = db.Column(db.String(64), nullable=False)
    detail         = db.Column(db.Text)
    segment_number = db.Column(db.Integer)
    extra_data     = db.Column(db.Text)   # JSON blob


def add_recording_event(recording_id, event_type, detail=None, segment_number=None, extra=None):
    """Insert a RecordingEvent into the session without committing - caller owns the commit.

    extra: optional dict, stored JSON-encoded in extra_data.
    """
    event = RecordingEvent(
        recording_id=recording_id,
        event_type=event_type,
        detail=detail,
        segment_number=segment_number,
        extra_data=json.dumps(extra) if extra else None,
    )
    db.session.add(event)
    return event


def preserve_cancelled_status(rec, detail):
    """Terminal-status guard for post-capture background work (dev/changelog/667).

    A user cancel can land while the concat/conversion chain is still running: the
    conversion registry drops its process handle in run_conversion_supervised's finally
    block, before the restart loop consumes the cancel flag, so a cancel arriving in that
    window finds nothing live, takes the stranded-row path, and marks the row ABORTED
    under a thread that is still working. When that thread reaches its own terminal write
    it must not resurrect the row into COMPLETED/FAILED/CONVERTING - a recording the user
    cancelled that later reports COMPLETED is exactly the unexplainable state the app
    exists to prevent.

    Returns True when the row is already ABORTED, so the caller skips its status write,
    after inserting an event naming what finished after the cancel. Insert-only: the
    caller's retry_on_locked closure owns the commit.
    """
    if rec is None or rec.status != REC_STATUS_ABORTED:
        return False
    add_recording_event(rec.id, RECORDING_ABORTED, detail=detail)
    return True


def detach_recording_references(recording_id: int):
    """Unlink every row that names a recordings.id but is not cascaded away with it.

    Insert-only in the same sense as add_recording_event above: mutates without
    committing, because the delete paths call this inside the same retry_on_locked
    closure that deletes the Recording row. A separate commit would leave a window where
    the row is gone and the rows naming it still point at its id.

    Still required even though recordings.id now carries AUTOINCREMENT (dev/changelog/937),
    and the two are belt and braces rather than alternatives. Before that, SQLite handed a
    deleted row's number straight to the next recording created, so anything still holding
    it did not merely dangle - it silently re-attached to an unrelated recording, and two
    alerts about deleted test recordings deep-linked to the recordings that inherited their
    numbers (dev/changelog/929). AUTOINCREMENT closes that for ids retired from here on; it
    cannot speak for an id already re-issued before the rebuild ran, or for one deleted from
    the top of the table beforehand, and it does nothing about a row naming a recording that
    simply no longer exists.

    RecordingEvent and RecordingSegment need nothing here: both relationships declare
    cascade='all, delete-orphan', so the ORM deletes them along with the row.
    """
    from .alerts import detach_recording_alerts
    detach_recording_alerts(recording_id)
    ChannelTest.query.filter_by(pre_check_recording_id=recording_id).update(
        {'pre_check_recording_id': None}, synchronize_session=False)


def group_event_channel_links(events) -> dict:
    """{event.id: [{'role': 'selected'|'to'|'from', 'channel': Channel, 'account': Account}]}
    for every GROUP_MEMBER_SELECTED/GROUP_FAILOVER event in `events` - lets a rendered event
    turn prose like 'recording from "FS1 (1080p)" (Provider A, ...)' into actual links to
    the channel/account named, using the channel id(s) the event's extra_data already carries.
    Shared by the recording detail page's own Event log and the group/channel Activity
    Timeline (routes/recordings.py, routes/channel_groups.py). Channels (with their account
    eager-loaded) are batch-fetched once for every event passed in, never per-row.
    """
    from sqlalchemy.orm import joinedload

    wanted = [e for e in events if e.event_type in (GROUP_MEMBER_SELECTED, GROUP_FAILOVER)]
    if not wanted:
        return {}

    parsed = {}
    channel_ids = set()
    for e in wanted:
        try:
            extra = json.loads(e.extra_data) if e.extra_data else {}
        except (ValueError, TypeError):
            extra = {}
        parsed[e.id] = extra
        for key in ('channel_id', 'to_channel_id', 'from_channel_id'):
            if extra.get(key) is not None:
                channel_ids.add(extra[key])

    if not channel_ids:
        return {}
    channels = {c.id: c for c in (Channel.query
                                   .options(joinedload(Channel.account))
                                   .filter(Channel.id.in_(channel_ids)).all())}

    links = {}
    for e in wanted:
        extra = parsed[e.id]
        rows = []
        if e.event_type == GROUP_MEMBER_SELECTED:
            ch = channels.get(extra.get('channel_id'))
            if ch:
                rows.append({'role': 'selected', 'channel': ch, 'account': ch.account})
        else:  # GROUP_FAILOVER
            from_ch = channels.get(extra.get('from_channel_id'))
            to_ch = channels.get(extra.get('to_channel_id'))
            if from_ch:
                rows.append({'role': 'from', 'channel': from_ch, 'account': from_ch.account})
            if to_ch:
                rows.append({'role': 'to', 'channel': to_ch, 'account': to_ch.account})
        if rows:
            links[e.id] = rows
    return links


class UserPref(db.Model):
    """Server-side per-user UI preferences (DESIGN.md 3.11: column setup and
    similar UI config persist server-side, never localStorage). Single-user
    app, so the key alone identifies a pref; value is a JSON blob."""
    __tablename__ = 'user_prefs'

    key   = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)


#: RecordingSegment.excluded_reason - what came back was the provider's finite "channel
#: offline" placeholder clip, not the channel (app/watchdog.py::classify_placeholder_segment).
SEGMENT_EXCLUDED_PLACEHOLDER = 'PROVIDER_PLACEHOLDER'


class RecordingSegment(db.Model):
    __tablename__ = 'recording_segments'

    id               = db.Column(db.Integer, primary_key=True)
    recording_id     = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=False, index=True)
    # Which member channel actually captured this segment. Recording.channel_id only ever
    # holds whichever member is CURRENT, which changes mid-recording under group failover -
    # this is what lets the segments table show each one's own channel. Null on segments
    # from before this column existed and never backfilled to a channel (dev/changelog/536-
    # adjacent work); always set going forward by _launch_segment.
    channel_id       = db.Column(db.Integer, db.ForeignKey('channels.id'))
    segment_number   = db.Column(db.Integer, nullable=False)
    file_path        = db.Column(db.String(1024), nullable=False)
    started_at       = db.Column(db.DateTime, nullable=False)
    ended_at         = db.Column(db.DateTime)
    # STOP_TIME_REACHED | STALL_KILLED | PROCESS_EXITED | SEGMENT_DURATION_EXPIRED | ERROR
    # | MANUAL_CANCEL | MANUAL_PAUSE | MANUAL_STOP | SERVICE_RESTART | RECORDING_HANDOFF
    # STALL_KILLED and PROCESS_EXITED are both "the feed died, we restarted", kept apart
    # because only the first one is us doing the killing (dev/changelog/431).
    exit_reason      = db.Column(db.String(64))
    bytes_recorded   = db.Column(db.Integer)
    stall_count      = db.Column(db.Integer, default=0)
    ffmpeg_exit_code = db.Column(db.Integer)
    # Capture-time stream format (light ffprobe run by the watchdog once the
    # segment has data) - describes the ORIGINAL capture, never the converted
    # output (DESIGN.md section 5). Null on pre-existing rows and on segments
    # that never produced enough data to probe.
    probe_resolution     = db.Column(db.String(32))   # e.g. "1920x1080"
    probe_fps            = db.Column(db.Float)
    probe_audio_codec    = db.Column(db.String(64))   # e.g. "aac"
    probe_audio_channels = db.Column(db.Integer)
    # Format profile of the same single ffprobe call above, so this costs no extra probe
    # (dev/changelog/330). Mirrors the ChannelTest quality-profile columns; informational
    # only, NEVER blended into health_score or failover ranking. These describe the
    # ORIGINAL capture, which is why they are worth keeping even when the recording is
    # later re-encoded to something else (Recording.recorded_* holds that output profile).
    # NULL means "we could not tell", and must stay distinguishable from a real value -
    # the watchdog probes a segment that is still GROWING, so a field absent from a partial
    # .ts header is left NULL rather than guessed. No bits-per-pixel-frame here for the
    # same reason: it needs a bitrate, and format-level bit_rate is unreliable mid-capture.
    probe_video_codec        = db.Column(db.String(32))  # ffprobe codec_name, e.g. "h264", "hevc"
    probe_pix_fmt            = db.Column(db.String(32))  # raw ffprobe value, e.g. "yuv420p10le"
    probe_bit_depth          = db.Column(db.Integer)     # parsed from pix_fmt (8/10/12)
    probe_chroma_subsampling = db.Column(db.String(8))   # "420" | "422" | "444", parsed from pix_fmt
    probe_interlaced         = db.Column(db.Boolean)     # field_order != progressive; NULL = unknown
    probe_coded_resolution   = db.Column(db.String(32))  # coded_width x coded_height, only when != resolution
    probe_is_vfr             = db.Column(db.Boolean)     # r_frame_rate vs avg_frame_rate mismatch; NULL = undetermined
    probed_at            = db.Column(db.DateTime)
    # How many seconds of content this segment's finished file actually holds, ffprobed
    # once at concat time (app/concatenator.py) rather than by the watchdog: the watchdog
    # sees a file that is still growing, so its duration would be whatever had arrived by
    # then. Compare it against the segment's own wall clock (ended_at - started_at) to see
    # a feed that delivered faster or slower than real time - a provider that replays its
    # buffer on reconnect opens every replacement segment with content the previous one
    # already had, so content exceeds wall clock (dev/changelog/942).
    # NULL means unknown and never zero: segments captured before this column existed are
    # not backfilled, because the only other source is a regex over a truncated stderr tail
    # and one column holding two measurement methods of differing precision is two answers
    # to one question.
    content_duration_seconds = db.Column(db.Float)
    # bytes_recorded/span far below the recording's own average bitrate - a timeline-clean
    # segment (frames present, evenly spaced) that is nonetheless a blank/slate screen.
    # app/postprocessor.py::_detect_near_empty_segments. NULL = not evaluated (predates this
    # detector, or gather_health_data was off), never a false "not near-empty".
    near_empty           = db.Column(db.Boolean)
    # Why this segment was kept out of the final file, or NULL for the normal case of a
    # segment that was joined. Deliberately NOT folded into exit_reason, which answers a
    # different question and stays true alongside it: a discarded placeholder really did end
    # with PROCESS_EXITED (one flag, one meaning). The row survives with its file path,
    # bytes and diagnostics intact - excluding beats deleting, because the discard has to be
    # explainable six weeks later from the recording's own detail page.
    excluded_reason      = db.Column(db.String(64))

    channel = db.relationship('Channel')

    @property
    def excluded(self) -> bool:
        """Was this segment kept out of the final file? The one spelling of that test, so a
        consumer never re-derives it from the reason string."""
        return self.excluded_reason is not None


# ── Account Models ────────────────────────────────────────────────────────────

# Account columns that carry provider credentials, enumerated once so the support bundle
# (app/support_bundle.py) and any future export iterate this constant rather than a
# hand-typed field list (DESIGN-secrets.md §4.3) - a new secret field on Account must be
# added here in the same change that introduces it.
SECRET_ACCOUNT_FIELDS = ('username', 'password', 'base_url', 'm3u_url', 'epg_url')


class Account(db.Model):
    """Base account: an IPTV provider connection, synced for channels + EPG.

    Single-table inheritance - M3uAccount and XtreamAccount add type-specific
    fields on top of this table, discriminated by account_type.
    """
    __tablename__ = 'accounts'

    id                = db.Column(db.Integer, primary_key=True)
    name              = db.Column(db.String(255), nullable=False)
    # account_type: 'm3u' (default) | 'xtream'
    account_type      = db.Column(db.String(32), nullable=False, default='m3u',
                                 server_default=db.text("'m3u'"))
    # M3U account fields
    m3u_url           = db.Column(db.String(2048))
    epg_url           = db.Column(db.String(2048))
    # Xtream account fields
    base_url          = db.Column(db.String(2048), nullable=False, default='')
    username          = db.Column(db.String(255), nullable=False, default='')
    password          = db.Column(db.String(255), nullable=False, default='')
    # UNSYNCED | SYNCING | OK | ERROR
    status            = db.Column(db.String(32), nullable=False, default='UNSYNCED')
    last_sync_at      = db.Column(db.DateTime)   # naive UTC
    next_sync_at      = db.Column(db.DateTime)   # naive UTC
    last_error        = db.Column(db.Text)
    channel_count     = db.Column(db.Integer, default=0)
    epg_entry_count   = db.Column(db.Integer, default=0)
    # How many of channel_count are currently hidden (app/channel_hiding.py). Refreshed
    # only by channel_hiding.refresh_hidden_channel_counts() - called from recompute()
    # itself, so it moves in the same unit as Channel.hidden - plus the missing-channel
    # sweep, which deletes hidden rows without going through recompute() at all.
    hidden_channel_count = db.Column(db.Integer, default=0, server_default=db.text('0'))
    # How many of channel_count's stream URLs were built by ChannelBin rather than
    # supplied by the provider (DESIGN-live-vod.md §4.3). 0 for M3U accounts, which
    # always get real URLs from their playlist. Backs the /accounts "Constructed URLs"
    # badge; refreshed every sync alongside channel_count/epg_entry_count.
    constructed_stream_url_count = db.Column(db.Integer, default=0, server_default=db.text('0'))
    color             = db.Column(db.String(32), nullable=False, default='#58a6ff')
    # None = use the global sync.url_normalization default; otherwise one of
    # accounts.NORM_* ('disabled' / 'mpegts' / 'mpegts_live' / 'hls').
    # Was a Boolean before _m014; legacy True/False rows are coerced by
    # accounts.coerce_normalization_mode, so read it through resolve_normalization_mode()
    # rather than testing it directly.
    url_normalization   = db.Column(db.String(16))
    # None = use global sync.sync_interval_hours; integer = per-account override
    sync_interval_hours = db.Column(db.Integer, nullable=True)
    # False = never auto-sync this account
    sync_enabled        = db.Column(db.Boolean, nullable=False, default=True,
                                    server_default=db.text('1'))
    # None = use global accounts.default_max_connections; integer = per-account override.
    # Shared cap on concurrent outbound connections to this provider across both
    # recordings and channel tests (see app/connection_limits.py). Account sync does
    # NOT count against this - it uses its own separate lock (app/accounts.py _sync_locks).
    max_connections      = db.Column(db.Integer, nullable=True)
    # The rest of this block is captured from the Xtream auth response's `user_info`/
    # `server_info` blocks (dev/changelog/534) - always None for M3U accounts, which have
    # no such response. Populated going forward only, refreshed every successful sync;
    # never backfilled for syncs that ran before this existed.
    provider_status       = db.Column(db.String(32))    # user_info.status, e.g. "Active"
    provider_exp_date     = db.Column(db.DateTime)       # user_info.exp_date, epoch -> naive UTC
    provider_is_trial     = db.Column(db.Boolean)        # user_info.is_trial
    provider_max_connections     = db.Column(db.Integer)  # user_info.max_connections
    # A snapshot of user_info.active_cons as of the last sync - not live.
    provider_active_connections  = db.Column(db.Integer)
    provider_allowed_output_formats = db.Column(db.String(255))  # comma-joined; display only,
    # never a rule for building stream URLs (DESIGN-live-vod.md §4.3).
    provider_stream_origin = db.Column(db.String(255))   # stream_origin_from_server_info() result
    # False = use the global debug.xtream_debug_mode flag only; True = the Fetch & Dump /
    # Sync from dump troubleshooting tools are also enabled for THIS account regardless of
    # the global flag (dev/changelog/547). Meaningless for M3U accounts (no such tooling
    # exists for them) but not type-restricted at the column level.
    xtream_debug_override = db.Column(db.Boolean, nullable=False, default=False,
                                      server_default=db.text('0'))
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    channels  = db.relationship('Channel', backref='account', lazy=True,
                                cascade='all, delete-orphan')
    sync_logs = db.relationship('AccountSyncLog', backref='account', lazy=True,
                                cascade='all, delete-orphan')

    __mapper_args__ = {
        'polymorphic_on': account_type,
        'polymorphic_identity': 'account',
    }


# Looks unused, but the polymorphic mapper requires a mapped subclass for every
# account_type discriminator value - removing it breaks loading of m3u rows.
class M3uAccount(Account):
    __mapper_args__ = {'polymorphic_identity': 'm3u'}


class XtreamAccount(Account):
    __mapper_args__ = {'polymorphic_identity': 'xtream'}


# ── Channel group format strategy ─────────────────────────────────────────────
#
# ChannelGroup.format_strategy - one standing setting deciding which format the group
# should be, NOT NULL so every reader is spared an `is None` branch
# (DESIGN-channel-groups-model.md 4.4). It writes the format lock; the lock then filters
# members at selection time and never mutates a stored participation choice.
#
# Only the four bucket-ranking strategies live in channel_groups.FORMAT_STRATEGIES - the
# other four are handled outside that engine.
GROUP_FORMAT_HEALTH_CHECK_ONLY = 'health_check_only'  # default: not a recording source
GROUP_FORMAT_HIGHEST_SCORE     = 'highest_score'      # reference follows the healthiest member
GROUP_FORMAT_HIGHEST_BITRATE   = 'highest_bitrate'
GROUP_FORMAT_HIGHEST_RESOLUTION = 'highest_resolution'
GROUP_FORMAT_MOST_CHANNELS     = 'most_channels'
GROUP_FORMAT_BALANCED          = 'balanced'
GROUP_FORMAT_MANUAL            = 'manual'             # the lock is whatever the user pinned
GROUP_FORMAT_UNMANAGED         = 'unmanaged'          # no format management at all

GROUP_FORMAT_STRATEGIES = (
    GROUP_FORMAT_HEALTH_CHECK_ONLY,
    GROUP_FORMAT_HIGHEST_SCORE,
    GROUP_FORMAT_HIGHEST_BITRATE,
    GROUP_FORMAT_HIGHEST_RESOLUTION,
    GROUP_FORMAT_MOST_CHANNELS,
    GROUP_FORMAT_BALANCED,
    GROUP_FORMAT_MANUAL,
    GROUP_FORMAT_UNMANAGED,
)

# ChannelGroup.muted_warnings - which of the group's warning banners the user has hidden
# (DESIGN-channel-groups-model.md 16.2). Only the three banners that describe a setup the
# user may have chosen deliberately are mutable; the two that describe a state nobody
# chose - nothing monitors these channels, and the strategy could not pick a format - are
# not, because hiding those would hide the reason the group is not doing what it looks
# like it does.
GROUP_WARNING_FORMAT   = 'format'    # mixed formats, and its unmanaged sibling
GROUP_WARNING_EPG      = 'epg'       # members carry different EPG ids (section 8)
GROUP_WARNING_OVERRIDE = 'override'  # no member matches the lock, so it is bypassed (15.2)

GROUP_WARNING_KINDS = (GROUP_WARNING_FORMAT, GROUP_WARNING_EPG, GROUP_WARNING_OVERRIDE)


class ChannelGroup(db.Model):
    """User-defined group of channels (see app/channel_groups.py): duplicate feeds of one
    logical channel, appearing in the TV Guide as a single row. Recordings created from it
    resolve to the highest-scored recording-enabled member at record start and can fail
    over to other members mid-recording (app/watchdog.py).

    There is one kind of group. A group used only for health checking is one whose
    format_strategy is health_check_only and whose members are all recording-disabled -
    a configuration, not a separate type (DESIGN-channel-groups-model.md DECIDED 2)."""
    __tablename__ = 'channel_groups'

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(255), nullable=False)
    # The pinned "TV Guide Channels" system group: membership computed dynamically
    # (in_guide + test_enabled) at run/display time, no stored membership rows, not
    # deletable.
    is_system        = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default=db.text('0'))
    in_guide         = db.Column(db.Boolean, nullable=False, default=False)
    # Shares the same numeric ordering space as Channel.guide_sort_order
    guide_sort_order = db.Column(db.Integer, default=0)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Group-as-a-whole lifetime health: total-churn quality of every group-backed recording
    # (all members combined), blended the same way as Channel.health_score - see
    # app/health_score.py::apply_recording_health_observation. Informational only; member
    # selection/failover still rank members by their individual effective_score.
    health_score               = db.Column(db.Float)     # None = not yet observed
    health_score_sample_count  = db.Column(db.Integer, nullable=False, default=0,
                                           server_default=db.text('0'))
    health_score_updated_at    = db.Column(db.DateTime)

    # Locked group format (Part C). Both NULL ⇒ reference is auto-derived from the best
    # member (today's behavior); both set ⇒ the group's format is pinned to this
    # resolution+fps. A partial pair (one set, one NULL) is treated as unlocked - always
    # set/clear the pair together; use locked_format_key/set_locked_format() below.
    format_resolution = db.Column(db.String(32))    # e.g. "1920x1080"
    format_fps        = db.Column(db.Integer)       # rounded int, e.g. 60
    # Which format this group should be, and therefore what writes the lock above. One of
    # GROUP_FORMAT_STRATEGIES; validate against that tuple server-side, never trust a
    # posted value. The lock filters members where they are chosen (guide row fill, record
    # start, failover) - it never writes ChannelGroupMember.recording_enabled, which is
    # user intent and is written only by a human (DESIGN-channel-groups-model.md 4.1).
    format_strategy   = db.Column(db.String(32), nullable=False,
                                  default=GROUP_FORMAT_HEALTH_CHECK_ONLY,
                                  server_default=db.text("'%s'" % GROUP_FORMAT_HEALTH_CHECK_ONLY))
    # Which warning banners the user has hidden for this group: a JSON list of
    # GROUP_WARNING_KINDS values, read and written through muted_warning_set /
    # set_muted_warnings below rather than directly.
    #
    # It lives on the group rather than in UserPref deliberately. The mute is a fact about
    # this group's setup, and a column dies with the row it belongs to - a UserPref keyed
    # on group id has to be deleted by hand in delete_group and in every other path that
    # can destroy a group, which is the teardown defect class CLAUDE.md already names.
    muted_warnings    = db.Column(db.Text)

    # Provenance note only (dev/changelog/501), stamped once by
    # clone_group() and never updated afterward - deliberately NOT a live pairing
    # (see `paired`/`attached_checks` above for that). cloned_from_name is a snapshot
    # taken at clone time so the note still reads correctly if the source is later
    # renamed or deleted; cloned_from_group_id links to it only while it still exists
    # (the source has no back-reference and can be freely deleted, converted or
    # re-cloned itself).
    cloned_from_group_id = db.Column(db.Integer, db.ForeignKey('channel_groups.id'), nullable=True)
    cloned_from_name      = db.Column(db.String(255), nullable=True)

    # The one membership store (channel_group_members join table). Each row's
    # .channel eager-loads (joined) so iterating a group's memberships is one query,
    # not one-per-member (CLAUDE.md no-N+1 rule).
    memberships = db.relationship('ChannelGroupMember',
                                  backref=db.backref('group', lazy='joined'),
                                  lazy=True, order_by='ChannelGroupMember.position',
                                  cascade='all, delete-orphan')
    # Deleting a group takes its events with it, per the teardown rule - they are
    # group-scoped and have nowhere else to belong.
    events      = db.relationship('ChannelGroupEvent', backref='group', lazy=True,
                                  order_by='ChannelGroupEvent.timestamp',
                                  cascade='all, delete-orphan')

    @property
    def locked_format_key(self):
        """The (resolution, fps) tuple when the group format is locked (both columns
        set), else None. A partial pair is treated as unlocked - matches format_key()'s
        tuple shape so it compares directly against member format keys."""
        if self.format_resolution and self.format_fps:
            return (self.format_resolution, int(self.format_fps))
        return None

    def set_locked_format(self, resolution, fps):
        """Lock the group format to (resolution, fps), or clear the lock when either is
        None/empty. Rejects a partial pair by clearing both - the lock is all-or-nothing."""
        if resolution and fps:
            self.format_resolution = resolution
            self.format_fps = int(fps)
        else:
            self.format_resolution = None
            self.format_fps = None

    def muted_warning_set(self):
        """The banner kinds hidden for this group, as a set of GROUP_WARNING_KINDS.

        Tolerant of a NULL, an empty string and anything that no longer parses: a warning
        surface that raises because a stored blob went bad would take the whole page with
        it, and the safe direction is showing a warning the user hid rather than hiding
        one they need. Unknown values are dropped rather than kept, so a kind that is
        later retired cannot resurrect itself."""
        if not self.muted_warnings:
            return set()
        try:
            stored = json.loads(self.muted_warnings)
        except (ValueError, TypeError):
            return set()
        if not isinstance(stored, list):
            return set()
        return {k for k in stored if k in GROUP_WARNING_KINDS}

    def set_muted_warnings(self, kinds):
        """Replace the hidden-banner set. Validates against GROUP_WARNING_KINDS and stores
        in a stable order, so an unchanged set never rewrites the column with a different
        string."""
        kept = [k for k in GROUP_WARNING_KINDS if k in set(kinds)]
        self.muted_warnings = json.dumps(kept) if kept else None


class Channel(db.Model):
    __tablename__ = 'channels'

    id               = db.Column(db.Integer, primary_key=True)
    # No index=True: uq_channel_account_stream below is (account_id, stream_id), and SQLite
    # seeks that index on account_id alone exactly as well - see RedundantPrefixIndexTests.
    account_id       = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    stream_id        = db.Column(db.Integer, nullable=False)
    name             = db.Column(db.String(512), nullable=False)
    logo_url         = db.Column(db.String(2048))
    # Local logo cache (app/logo_cache.py). logo_cache_path is the cached file's name
    # under recording.logo_cache.dir, NULL until fetched (or if the fetch failed/wasn't
    # an image). logo_cache_source_url is the logo_url the cache was last built/attempted
    # from - comparing it to the current logo_url is the only change-detection check, so
    # a provider that never changes its logo URL never gets re-fetched.
    logo_cache_path        = db.Column(db.String(255))
    logo_cache_source_url  = db.Column(db.String(2048))
    category_name    = db.Column(db.String(255))
    category_id      = db.Column(db.String(64))
    stream_url       = db.Column(db.String(2048), nullable=False)   # normalized recording URL
    raw_stream_url   = db.Column(db.String(2048))                   # original URL from M3U/API
    epg_channel_id   = db.Column(db.String(255), index=True)
    in_guide         = db.Column(db.Boolean, nullable=False, default=False)
    guide_sort_order = db.Column(db.Integer, default=0)
    test_enabled     = db.Column(db.Boolean, nullable=False, default=True,
                                 server_default=db.text('1'))
    is_duplicate_stream_url = db.Column(db.Boolean, nullable=False, default=False,
                                        server_default=db.text('0'))
    # Whether this channel's provider URL carries a <user>/<pass>/<numeric id> triplet for
    # URL normalization to rebuild from - app/accounts.py::url_is_normalizable() of
    # raw_stream_url, stamped in _upsert_channels alongside the URL it describes. Stored
    # rather than derived because the check is a Python regex over a 2KB string and the
    # channel search filters on it: deriving it needed a full-table fetch plus 136k regex
    # matches (~1.3s measured) per request, which is exactly the hidden-I/O shape CLAUDE.md
    # forbids. False means normalization deliberately left the URL untouched (usually an
    # Icecast radio mount or a placeholder, ~850 of 136k channels here).
    url_normalizable = db.Column(db.Boolean, nullable=False, default=True,
                                 server_default=db.text('1'))
    # ── Hiding (app/channel_hiding.py, dev/changelog/775) ─────────────────────
    #
    # hidden is the MATERIALIZED EFFECTIVE ANSWER and means exactly "do not offer this
    # channel", nothing else. It is the only one of these four any read site touches, it is
    # written only by channel_hiding.recompute(), and it must never come to also mean "the
    # user hid this" - that is hidden_override. Conflating the two is the shape of the
    # in_guide defect this file documents at length.
    #
    # Hiding decides where a channel is OFFERED and nothing more: it never stops a
    # recording, never removes a guide row, never touches group membership, and never
    # affects which member a group records from.
    #
    # Deliberately NOT indexed (migration 45 dropped the one migration 43 added). Every SQL
    # predicate here asks for the COMMON value - hidden = 0, "the channels you are offered" -
    # which no index can answer usefully, and filtering hidden rows was measured free without
    # one. An index costs a write on all 137,144 rows per upsert and per materialize, and on
    # simpler query shapes the planner will prefer it over ix_channels_lower_name and lose
    # the ORDER BY (dev/changelog/781).
    hidden           = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default=db.text('0'))
    # The human's own answer, and the only column here a person writes: NULL = follow the
    # rules, True = force-hidden whatever they say, False = force-shown whatever they say.
    # A user's answer to a judgment call, so the participation-switch rule applies - no
    # background job, sweep or reconcile pass may write it, and the one writer is
    # channel_hiding.set_hidden_override().
    hidden_override  = db.Column(db.Boolean)
    # Which source produced the current answer, for display - one of
    # channel_hiding.HIDE_REASONS. NULL when the channel is not hidden and nothing wanted
    # it hidden.
    hidden_reason    = db.Column(db.String(32))
    # A source said hide, but the channel is in the TV Guide or in a channel group and is
    # being kept visible until that clears. Guide/group membership DEFERS a hide, it never
    # refuses one: the intent is recorded and honored the moment the blocker goes away.
    hidden_deferred  = db.Column(db.Boolean, nullable=False, default=False,
                                 server_default=db.text('0'))
    notes            = db.Column(db.Text)
    # Channel lifecycle tracking (DESIGN-sync-resilience.md §5): first_seen_at is set once
    # at row creation; last_seen_at is updated on every sync whose feed includes the
    # channel (both stamped in app/accounts.py::_upsert_channels, the one shared M3U/
    # Xtream choke point). Feeds the "missing from provider" / "new" derived display
    # states (never stored) - see app/accounts.py::channel_lifecycle_state().
    first_seen_at    = db.Column(db.DateTime)
    last_seen_at     = db.Column(db.DateTime, index=True)
    # ── Lifetime health score (decay-weighted, fed by both channel tests and
    # recordings - see app/health_score.py) ────────────────────────────────
    health_score               = db.Column(db.Float)     # None = not yet observed
    health_score_sample_count  = db.Column(db.Integer, nullable=False, default=0,
                                           server_default=db.text('0'))
    health_score_updated_at    = db.Column(db.DateTime)  # timestamp of the observation that produced the current score
    manual_health_adjustment   = db.Column(db.Integer, nullable=False, default=0,
                                           server_default=db.text('0'))  # bounded -100..+100
    manual_health_note         = db.Column(db.Text)
    # Trailing count of consecutive FAILED ChannelTest rows (CANCELLED skipped, neither
    # extending nor resetting it - not the channel's fault, mirrors score_test_quality's
    # own CANCELLED exclusion), maintained by health_score.py::apply_test_health_observation.
    # A distinct signal from health_score on purpose (dev/changelog/478): the decay-weighted
    # blend can leave a channel with a good history above the failing band through
    # weeks of hard failures. Earns its own column per CLAUDE.md (sorted on in
    # channel_groups.rank_members, displayed via channel_failing_reason).
    consecutive_test_failures = db.Column(db.Integer, nullable=False, default=0,
                                          server_default=db.text('0'))
    # None = no default profile; guide record modal falls back to "None" selection for this channel
    default_profile_id = db.Column(db.Integer, db.ForeignKey('recording_profiles.id'), nullable=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Moves only when a column the channels search index actually contains changes - ch_fts
    # covers name, stream_url, epg_channel_id and category_name, nothing else. The index's
    # staleness watermark reads MAX() of this (app/search_index.py::_SOURCE_WATERMARK_SQL),
    # so a write touching any OTHER column of this row must leave it alone.
    #
    # That is the entire reason it exists rather than reusing updated_at, which moves for a
    # health score from a channel test, an in_guide toggle, a default-profile change. None of
    # those change a token in ch_fts, but keying the watermark off updated_at would call the
    # index stale for every one of them - and nothing repairs staleness except a sync's own
    # rebuild, so a 3am channel test would degrade every search until the next sync hours
    # later (dev/changelog/674).
    #
    # NULL is a valid value and needs no backfill: it reads as "nothing the index covers has
    # changed since this column existed", and a watermark only ever has to match itself.
    #
    # **Any new writer of those four columns must stamp this.** Today that is
    # accounts.py::_upsert_channels and routes/accounts.py::_renormalize_chunk.
    search_text_updated_at = db.Column(db.DateTime, index=True)

    default_profile = db.relationship('RecordingProfile', foreign_keys=[default_profile_id], lazy='joined')
    # All group memberships. Membership has no bearing on this channel's own guide row:
    # in_guide means "this channel is a guide row" and nothing else, and a member may hold
    # one alongside its group's (dev/changelog/751). The membership row's .group
    # eager-loads (joined) so iterating a channel's memberships is one query.
    group_memberships = db.relationship('ChannelGroupMember',
                                        backref=db.backref('channel', lazy='joined'),
                                        lazy=True, cascade='all, delete-orphan')
    epg_entries = db.relationship('EPGEntry', backref='channel', lazy=True,
                                  cascade='all, delete-orphan')
    tests       = db.relationship('ChannelTest', backref='channel', lazy=True,
                                  order_by='ChannelTest.test_started_at',
                                  cascade='all, delete-orphan')
    events      = db.relationship('ChannelEvent', backref='channel', lazy=True,
                                  order_by='ChannelEvent.timestamp',
                                  cascade='all, delete-orphan')
    # Cascaded like the two above rather than left to the database: foreign keys are OFF in
    # this app's SQLite, so a deleted channel leaves anything not cascaded here behind, and
    # ids are reused (CLAUDE.md teardown rule).
    health_exclusions = db.relationship('ChannelHealthExclusion', backref='channel',
                                        lazy=True, cascade='all, delete-orphan')

    # Declared here as well as in migration 24, because a FRESH database never runs
    # migrations - run_migrations() stamps it at CURRENT_SCHEMA_VERSION and returns - so an
    # index that exists only in a migration is an index new installs silently do without
    # (the same gap EPGEntry.__table_args__ above documents, missed here until it was found
    # dev/changelog/695). Names/columns must match _m024_channel_search_support exactly, or
    # CREATE INDEX IF NOT EXISTS on a migrated DB silently no-ops against the wrong definition.
    __table_args__ = (
        db.UniqueConstraint('account_id', 'stream_id', name='uq_channel_account_stream'),
        db.Index('ix_channels_category_name', 'category_name'),
        # Two boolean-led indexes, kept because their queries seek the RARE value - which is
        # the whole distinction BooleanLeadingIndexTests exists to make. Measured on the
        # production database: in_guide=1 is 0.0 ms here against 171.4 ms without,
        # is_duplicate_stream_url=1 is 1.0 ms against 40.5 ms. Seeking the COMMON value of
        # either would cost ~139 ms against 0.2 ms, and no query does. That is what
        # ix_channels_hidden and ix_channels_test_enabled could not say for themselves, which
        # is why migration 45 dropped them (dev/changelog/781).
        db.Index('ix_channels_in_guide', 'in_guide'),
        db.Index('ix_channels_is_duplicate_stream_url', 'is_duplicate_stream_url'),
        # The standing breakdown's covering index, and the one boolean-led index here that is
        # never SOUGHT - it is SCANNED end to end, which is a third case the rare-vs-common
        # rule above does not cover. `_standing_breakdown_compute()` buckets every channel
        # through one ordered CASE, so it reads a handful of narrow columns off all 137,283
        # rows of a 63 MB table and the scan, not the predicates, is the cost. Holding those
        # columns in a 1.8 MB index lets SQLite answer the whole statement without touching
        # the table: measured on a copy of the production database, 181.9 ms -> 99.7 ms on
        # the query alone and 219.5 ms -> 120.7 ms end to end over HTTP, with
        # `channels no-q rows` (which runs the same breakdown) 268.8 ms -> 168.8 ms - and no
        # decoy plan anywhere, since the default sort's own query got FASTER, not slower
        # (dev/changelog/834).
        #
        # The column list is not free-form: it must hold every `channels` column the
        # channel-side predicates in `channel_search.py::_standing_reject()` read, or the
        # index silently stops covering and the scan returns. StandingIndexCoverageTests
        # checks that rather than trusting this comment.
        db.Index('ix_channels_standing', 'hidden', 'in_guide', 'url_normalizable',
                 'account_id', 'health_score'),
        # health_score, manual_health_adjustment together: the health band shown in the UI
        # is the *effective* score (health_score + manual_health_adjustment), so an index on
        # health_score alone would not cover the aggregate and SQLite would fall back to the
        # table (migration 24's own comment).
        db.Index('ix_channels_health', 'health_score', 'manual_health_adjustment'),
        # The channel grain's DEFAULT sort key, so the landing page can walk this index and
        # stop at LIMIT 100 instead of pouring every surviving row into a temp B-tree:
        # measured 105.5ms -> 9.0ms on 138k channels (dev/changelog/699). The expression must
        # stay spelled `lower(name)`, character for character, to match what
        # channel_search.py::SORTS orders by - SQLite matches an expression index by the text
        # of the expression, and `name COLLATE NOCASE` would silently not match at all.
        #
        # The write side is why this is affordable: `last_seen_at`, the one column every sync
        # stamps on every row, is not in it, so a steady-state re-sync does no index
        # maintenance here (0.63s vs 0.64s over 57,173 rows).
        db.Index('ix_channels_lower_name', db.func.lower(name), 'id'),
    )


class ChannelGroupMember(db.Model):
    """Many-to-many group membership (DESIGN-groups-unification.md): a channel may
    belong to any number of groups, no cap. Per-member state lives here, not on the
    channel - one feed can record in group A and not in group B."""
    __tablename__ = 'channel_group_members'

    id         = db.Column(db.Integer, primary_key=True)
    # No index=True: uq_group_member below is (group_id, channel_id), which answers a
    # group_id seek itself - see RedundantPrefixIndexTests.
    group_id   = db.Column(db.Integer, db.ForeignKey('channel_groups.id'), nullable=False)
    channel_id = db.Column(db.Integer, db.ForeignKey('channels.id'),
                           nullable=False, index=True)
    # Order within the group. Ranking for selection is by effective score, not by this.
    position   = db.Column(db.Integer, nullable=False, default=0)
    # ── The two participation columns (DESIGN-channel-groups-model.md 4.2) ──
    #
    # Both are written by a human and by nothing else. No engine may set them: the
    # format lock filters at selection time instead, so a member that starts matching
    # again is eligible again with no re-enable machinery (4.1). A change to either
    # writes a GROUP_MEMBER_PARTICIPATION ChannelGroupEvent, which is why the one writer
    # is channel_groups.py::set_participation() - a route that assigned the column
    # itself moved a switch with no trace anywhere (dev/changelog/748).
    #
    # recording_enabled: eligible to serve the group's guide row, to be picked at record
    # start, and to be failed over to. Defaults False - a group is created as a health
    # check and promoted deliberately (14), so nothing records until a human says so.
    recording_enabled = db.Column(db.Boolean, nullable=False, default=False,
                                  server_default=db.text('0'))
    # test_enabled: included in this group's health check runs. Defaults True, the other
    # half of that same default.
    #
    # The name collision with Channel.test_enabled is deliberate and the two compose
    # rather than compete: the channel-wide switch is an off switch and it wins, this one
    # scopes participation to one group, and a run tests a member when BOTH are on. Code
    # reading either must be explicit about which object it is on.
    test_enabled      = db.Column(db.Boolean, nullable=False, default=True,
                                  server_default=db.text('1'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('group_id', 'channel_id', name='uq_group_member'),
    )


class ChannelGroupEvent(db.Model):
    """Group-scoped activity, merged into the group's Activity Timeline
    (DESIGN-channel-groups-model.md 4.5).

    channel_id is nullable because some facts are about the group as a whole (the lock
    moved) and some are about one membership (a checkbox changed); when set, the timeline
    renders it as the entry's source. See the GROUP_* event type constants above for what
    belongs here rather than on ChannelEvent."""
    __tablename__ = 'channel_group_events'

    id         = db.Column(db.Integer, primary_key=True)
    # No index=True on group_id: ix_channel_group_events_group_ts below leads with it,
    # and this table is only ever read newest-first per group - see
    # RedundantPrefixIndexTests.
    group_id   = db.Column(db.Integer, db.ForeignKey('channel_groups.id'), nullable=False)
    channel_id = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=True)
    timestamp  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    event_type = db.Column(db.String(64), nullable=False)
    detail     = db.Column(db.Text)
    extra_data = db.Column(db.Text)   # JSON blob

    __table_args__ = (
        db.Index('ix_channel_group_events_group_ts', 'group_id', 'timestamp'),
    )


class ChannelTest(db.Model):
    __tablename__ = 'channel_tests'

    id              = db.Column(db.Integer, primary_key=True)
    # No index=True: ix_channel_tests_channel_started below leads with channel_id, and every
    # read of this column wants the newest test first anyway - see RedundantPrefixIndexTests.
    channel_id      = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=False)
    job_id          = db.Column(db.Integer, db.ForeignKey('on_demand_test_jobs.id'),
                                nullable=True, index=True)   # NULL = guide test; set = on-demand
    # Set when this test was a pre-recording health check (DESIGN-prerecord-checks.md §3),
    # not a reuse of job_id (that means "belongs to an OnDemandTestJob").
    pre_check_recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'),
                                nullable=True, index=True)
    test_started_at = db.Column(db.DateTime, nullable=False)   # naive UTC
    test_ended_at   = db.Column(db.DateTime)                   # naive UTC; null while in progress
    # COMPLETED | FAILED | CANCELLED - see TEST_STATUS_* above
    status          = db.Column(db.String(32), nullable=False, default=TEST_STATUS_FAILED)
    connected       = db.Column(db.Boolean)
    resolution      = db.Column(db.String(32))      # e.g. "1280x720"
    fps             = db.Column(db.Float)
    bitrate_kbps    = db.Column(db.Float)
    drop_count       = db.Column(db.Integer, default=0)
    duration_seconds = db.Column(db.Float)       # actual recorded clip length from ffprobe
    frame_count      = db.Column(db.Integer)     # actual frames captured (ffprobe -count_packets)
    frame_pct        = db.Column(db.Float)       # frame_count / (fps * duration_seconds) * 100
    connect_attempts = db.Column(db.Integer, default=1, server_default=db.text('1'))
    screenshot_path  = db.Column(db.String(1024))
    screenshot_pruned = db.Column(db.Boolean, nullable=False, default=False,
                                  server_default=db.text('0'))  # file deleted by retention policy
    audio_codec        = db.Column(db.String(64))
    audio_channels     = db.Column(db.Integer)
    audio_sample_rate  = db.Column(db.Integer)
    audio_bitrate_kbps = db.Column(db.Float)
    audio_language     = db.Column(db.String(32))
    # ── Stream quality profile (DESIGN-stream-quality-profile.md) ─────────────
    # v1 = informational only; NEVER blended into health_score or failover ranking.
    video_codec          = db.Column(db.String(32))   # ffprobe codec_name, e.g. "h264", "hevc"
    pix_fmt              = db.Column(db.String(32))    # raw ffprobe value, e.g. "yuv420p10le"
    bit_depth            = db.Column(db.Integer)       # parsed from pix_fmt (8/10/12)
    chroma_subsampling   = db.Column(db.String(8))     # "420" | "422" | "444", parsed from pix_fmt
    interlaced           = db.Column(db.Boolean)       # field_order != progressive; NULL = unknown
    coded_resolution     = db.Column(db.String(32))    # coded_width x coded_height, only when != resolution
    is_vfr               = db.Column(db.Boolean)       # r_frame_rate vs avg_frame_rate mismatch; NULL = undetermined
    bits_per_pixel_frame = db.Column(db.Float)         # bitrate_bps / (w*h*fps) - efficiency stat, not a verdict
    timeline_gap_count   = db.Column(db.Integer)       # from scan_video_timeline on the test clip
    timeline_gap_seconds = db.Column(db.Float)
    error_detail     = db.Column(db.Text)
    quality_score        = db.Column(db.Integer)  # this test's own 0-100 health-score contribution, frozen at computation time
    lifetime_score_after = db.Column(db.Integer)  # snapshot of Channel.health_score immediately after this test was folded in
    quality_breakdown    = db.Column(db.Text)      # JSON: per-penalty math behind quality_score
    blend_breakdown      = db.Column(db.Text)      # JSON: decay/blend math behind lifetime_score_after
    # ── Multi-track detection (dev/changelog/565) ──────────────────────────────
    # video_codec/audio_codec above always describe track 0; these cover tracks beyond
    # that. Counts are columns (sortable/filterable); extra_tracks is JSON since a
    # per-track list can't be flattened into scalar columns - same shape as
    # quality_breakdown/blend_breakdown above.
    video_track_count    = db.Column(db.Integer)   # total video streams ffprobe found
    audio_track_count    = db.Column(db.Integer)   # total audio streams ffprobe found
    extra_tracks         = db.Column(db.Text)      # JSON: [{type, codec, language, ...}] for every track beyond the first of its type

    __table_args__ = (
        db.Index('ix_channel_tests_channel_started', 'channel_id', 'test_started_at'),
    )


class ChannelEvent(db.Model):
    __tablename__ = 'channel_events'

    id         = db.Column(db.Integer, primary_key=True)
    # No index=True: ix_channel_events_channel_ts below leads with channel_id, and this
    # table is only ever read newest-first per channel - see RedundantPrefixIndexTests.
    channel_id = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=False)
    timestamp  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    event_type = db.Column(db.String(64), nullable=False)
    detail     = db.Column(db.Text)
    extra_data = db.Column(db.Text)   # JSON blob, e.g. {'old_adjustment': -10, 'new_adjustment': 20}

    __table_args__ = (
        db.Index('ix_channel_events_channel_ts', 'channel_id', 'timestamp'),
    )


class ChannelHealthExclusion(db.Model):
    """One observation the user has taken out of a channel's health score.

    `Channel.health_score` is a lossy exponential average, so an observation cannot be
    subtracted back out - undoing one is a replay of the rest (app/health_recompute.py).
    This table is what a replay reads to know which ones to skip.

    Exclusion rather than deletion is deliberate (dev/changelog/895): an observation may be
    a Recording, and destroying a recording to unwind its effect on a score would be a far
    larger act than the one the user asked for. Every excluded observation stays on the
    Activity Timeline, marked as not counted.

    (source_kind, source_id) addresses the row it came from - see health_recompute.py's
    SOURCE_* constants. The kind is part of the key because one Recording contributes two
    separately-excludable observations (its capture, and the post-process correction).
    """
    __tablename__ = 'channel_health_exclusions'

    id          = db.Column(db.Integer, primary_key=True)
    channel_id  = db.Column(db.Integer, db.ForeignKey('channels.id'), nullable=False)
    source_kind = db.Column(db.String(32), nullable=False)
    source_id   = db.Column(db.Integer, nullable=False)
    excluded_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: 'reset' or 'step_back' - which action excluded it, for the timeline's benefit.
    action      = db.Column(db.String(16), nullable=False)

    __table_args__ = (
        # UNIQUE, not merely an index: excluding the same observation twice would make the
        # step-back count disagree with the replay, and every writer already checks first.
        db.UniqueConstraint('channel_id', 'source_kind', 'source_id',
                            name='uq_health_exclusion'),
    )


#: `ChannelHideRule.target` - what the pattern is matched against, and how.
HIDE_TARGET_CATEGORY_GLOB  = 'category_glob'
HIDE_TARGET_CATEGORY_EXACT = 'category_exact'
HIDE_TARGET_NAME_GLOB      = 'name_glob'

HIDE_TARGETS = (HIDE_TARGET_CATEGORY_GLOB, HIDE_TARGET_CATEGORY_EXACT, HIDE_TARGET_NAME_GLOB)


class ChannelHideRule(db.Model):
    """One blanket rule that takes channels out of the way - sources 1, 2 and 3 of the four
    that stack into `Channel.hidden` (app/channel_hiding.py, dev/docs/DESIGN-channel-hiding.md).

    Rules are DATA, not configuration: they carry per-row statistics and timestamps and are
    edited from the UI one at a time, which is the wrong shape for config.yaml's
    whole-file atomic replace. Nothing about them reaches `app/config.py`.

    `account_id` NULL means the rule is global - it applies to every account's channels.
    One nullable column rather than two lists, because "global" and "account 3 only" are the
    same question asked at different scopes, and most rules land per account: three of the
    four accounts here prefix their categories differently (`DE: `, `RO| `, `UK| `).

    Category rules match `Channel.category_name`, never `category_id`. Measured across four
    real accounts: `category_id` is populated on exactly one of them and empty on two of the
    three Xtream accounts, so the name is the only identifier present everywhere. The cost is
    that a provider renaming a category silently un-matches an exact pick.

    `match_count` / `deferred_count` / `counted_at` are a display cache refreshed by
    `channel_hiding.refresh_rule_stats()`, never the authority for what is hidden - that is
    `Channel.hidden`, and only `channel_hiding.recompute()` writes it. A NULL `counted_at`
    means the rule has never been counted, which is different from a count of zero.
    """
    __tablename__ = 'channel_hide_rules'

    id         = db.Column(db.Integer, primary_key=True)
    # No index=True: uq_hide_rule_scoped below leads with account_id, so a scoped lookup
    # seeks it (see RedundantPrefixIndexTests). The one shape it cannot answer - the global
    # list, WHERE account_id IS NULL - is a table this app measures in dozens of rows, so a
    # standalone index would cost a write on every rule edit to save nothing.
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'))
    target     = db.Column(db.String(32), nullable=False)
    pattern    = db.Column(db.String(512), nullable=False)
    enabled    = db.Column(db.Boolean, nullable=False, default=True,
                           server_default=db.text('1'))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime)
    # Display cache - see the class docstring.
    match_count    = db.Column(db.Integer)
    deferred_count = db.Column(db.Integer)
    counted_at     = db.Column(db.DateTime)

    __table_args__ = (
        # Two PARTIAL indexes rather than one plain UNIQUE, because SQLite treats NULLs as
        # distinct: a single UNIQUE (account_id, target, pattern) would let two identical
        # GLOBAL rules coexist, since their account_id is NULL both times. Splitting on
        # account_id IS NULL gives both scopes a real uniqueness argument, which is what the
        # keyed-lookup rule asks for before anything builds a dict off the pair.
        db.Index('uq_hide_rule_scoped', 'account_id', 'target', 'pattern',
                 unique=True, sqlite_where=db.text('account_id IS NOT NULL')),
        db.Index('uq_hide_rule_global', 'target', 'pattern',
                 unique=True, sqlite_where=db.text('account_id IS NULL')),
    )


class OnDemandTestJob(db.Model):
    __tablename__ = 'on_demand_test_jobs'

    id                   = db.Column(db.Integer, primary_key=True)
    name                 = db.Column(db.String(512), nullable=False)
    # The single pinned "TV Guide Channels" system job: its channel list is resolved
    # dynamically at run/display time via its is_system group - one channel per guide row
    # plus one per group with no schedule of its own, minus any whose channel-wide
    # test_enabled is off (channel_groups.system_check_targets, dev/changelog/752). It
    # can't be deleted or have channels managed directly.
    is_system            = db.Column(db.Boolean, nullable=False, default=False,
                                     server_default=db.text('0'))
    # QUEUED | SCHEDULED | RUNNING | COMPLETED | CANCELLED
    status               = db.Column(db.String(32), nullable=False, default='QUEUED')
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)
    scheduled_start_time = db.Column(db.DateTime)   # naive UTC; next run when SCHEDULED (recurring or not)
    completed_at         = db.Column(db.DateTime)   # last-run time when recurring; terminal-run time otherwise
    recurring            = db.Column(db.Boolean, nullable=False, default=False,
                                     server_default=db.text('0'))
    recur_day            = db.Column(db.Integer)  # 0=every day, 1=Sun...7=Sat - same encoding as channel_testing.test_days
    recur_hour           = db.Column(db.Integer)  # 0-23
    recur_minute         = db.Column(db.Integer)  # 0-59
    recur_paused         = db.Column(db.Boolean, nullable=False, default=False,
                                     # recurring settings kept, but no active APScheduler job
                                     server_default=db.text('0'))
    status_before_schedule = db.Column(db.String(32))  # status to restore to on Unschedule (QUEUED/COMPLETED/CANCELLED)
    # Maintenance window (app/check_window.py): dispatcher-owned instead of an exact
    # recur_hour:recur_minute CronTrigger. recur_hour/recur_minute stay populated when
    # this is True (not nulled) so toggling window mode off restores the previous time.
    recur_use_window    = db.Column(db.Boolean, nullable=False, default=False,
                                    server_default=db.text('0'))
    # Set only when a run finishes untruncated (never on a hard-stop). The due_jobs()
    # ordering key - a hard-stopped check keeps its stale value and floats to the front
    # of the next window instead of losing its turn twice.
    last_full_run_at    = db.Column(db.DateTime)
    # "Skip next run" for a window job, which has no APScheduler job to modify_job.
    window_skip_until   = db.Column(db.DateTime)
    # Groups unification 3/4: the job's channel list IS its group's membership
    # (channel_group_members, ordered by position). Nullable at the column level for
    # SQLite ADD COLUMN; app-enforced NOT NULL after _m011's backfill. Several jobs may
    # attach to one group (e.g. a nightly quick check + a weekly deep check).
    group_id             = db.Column(db.Integer, db.ForeignKey('channel_groups.id'))
    scheduler_job_id     = db.Column(db.String(255))            # APScheduler job ID if SCHEDULED
    profile_id           = db.Column(db.Integer, db.ForeignKey('health_check_profiles.id'))  # None = use global channel_testing.* config

    tests = db.relationship('ChannelTest', backref='job', lazy='dynamic',
                            foreign_keys='ChannelTest.job_id')
    profile = db.relationship('HealthCheckProfile', lazy='joined')
    group = db.relationship('ChannelGroup', lazy='joined',
                            backref=db.backref('test_jobs', lazy=True))


class EPGEntry(db.Model):
    __tablename__ = 'epg_entries'

    id          = db.Column(db.Integer, primary_key=True)
    # A strict prefix of ix_epg_entries_channel_stop, and KEPT ANYWAY - the one measured
    # exception to RedundantPrefixIndexTests, which carries it in _MEASURED_PREFIX_EXEMPTIONS
    # rather than inferring it. Dropping it does not make SQLite fall through to the wider
    # index: the airing grain's group-dedup subquery needs title/start_time/stop_time as well
    # as channel_id, so with only the wider index available the planner builds an AUTOMATIC
    # PARTIAL COVERING INDEX over epg_entries on every query instead. The default airings page
    # goes 0.098s to 2.914s on the production database (dev/changelog/692).
    channel_id  = db.Column(db.Integer, db.ForeignKey('channels.id'),
                             nullable=False, index=True)
    title       = db.Column(db.String(512), nullable=False)
    sub_title   = db.Column(db.String(512))
    description = db.Column(db.Text)
    # No index=True: ix_epg_entries_start_stop below is (start_time, stop_time). A standalone
    # start_time index is not merely redundant here, it is a decoy - see that index's comment.
    start_time  = db.Column(db.DateTime, nullable=False)               # naive UTC
    stop_time   = db.Column(db.DateTime, nullable=False, index=True)
    category    = db.Column(db.String(255))
    rating      = db.Column(db.String(64))
    # VIRTUAL generated column, not app-written - SQLite computes it from start_time/stop_time
    # on every read, so it can never disagree with them and the EPG sync path never touches it.
    # Backs the EPG Deep Search program-length filter (migration 36, dev/changelog/594).
    duration_minutes = db.Column(db.Integer, db.Computed(
        'CAST((julianday(stop_time) - julianday(start_time)) * 1440 AS INTEGER)',
        persisted=False))

    # Declared here as well as in their migrations, because a FRESH database never runs
    # migrations - run_migrations() stamps it at CURRENT_SCHEMA_VERSION and returns - so an
    # index that exists only in a migration is an index new installs silently do without.
    # ix_epg_entries_channel_stop was in exactly that state until 2026-07-31 (migration 17).
    __table_args__ = (
        # The account page's per-account EPG-match diagnostic
        # (accounts.py::_epg_match_counts, migration 17).
        db.Index('ix_epg_entries_channel_stop', 'channel_id', 'stop_time'),
        # The airing grain's first page: `stop_time > now ORDER BY start_time LIMIT n`. Both
        # columns in one index let SQLite skip the already-ended prefix off the index instead
        # of reading a table row per candidate - 1,716ms to 127ms on the production database
        # (migration 25, dev/changelog/414).
        #
        # This index is only reliably CHOSEN because migration 39 deleted the standalone
        # ix_epg_entries_start_time that used to sit beside it. While both existed, the
        # planner could be tipped onto the narrow one by an edit with no semantic content at
        # all - removing a duplicated ORDER BY term was enough - and the narrow index cannot
        # answer `stop_time` from the index, so every one of the ~1.16M already-ended
        # showings cost a table row fetch: 0.091s to 1.222s for 100 rows. Do not reintroduce
        # a standalone start_time index (dev/changelog/692).
        db.Index('ix_epg_entries_start_stop', 'start_time', 'stop_time'),
        # The program-length filter's index. Unindexed, a duration BETWEEN scan is a full
        # table scan (11.26s measured on 714,553 rows); indexed, SQLite plans it as a normal
        # b-tree range search (0.02s, 562x) - migration 36, dev/changelog/594.
        db.Index('ix_epg_entries_duration', 'duration_minutes'),
    )


class Tag(db.Model):
    """A named, colored, reusable text classifier - e.g. detecting stylized 'Live'/'New'
    markers in EPG data. A Tag itself carries no baked-in purpose: how it's used (insert its
    name, clean matched text out of a filename, highlight/filter in the TV Guide) is decided
    at the point of use, not on this record."""
    __tablename__ = 'tags'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(255), nullable=False, unique=True)
    color      = db.Column(db.String(32), nullable=False, default='#58a6ff')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    patterns = db.relationship('TagPattern', backref='tag', lazy=True,
                               cascade='all, delete-orphan')


class TagPattern(db.Model):
    """One literal match string for a Tag. A tag matches if ANY of its patterns is found
    (case-insensitive substring) in the checked text - multiple patterns per tag support
    providers that stylize the same marker differently."""
    __tablename__ = 'tag_patterns'

    id      = db.Column(db.Integer, primary_key=True)
    tag_id  = db.Column(db.Integer, db.ForeignKey('tags.id'), nullable=False, index=True)
    pattern = db.Column(db.String(255), nullable=False)


class SavedSearch(db.Model):
    """A user-saved TV Guide search term - shared between the instant filter bar and the
    extended search modal. Case is preserved as typed; dedupe is a case-insensitive check
    done in the route, not a DB constraint (unlike Tag.name, saved searches aren't forced
    to lowercase). Column is named query_text, not query - `query` is reserved by
    Flask-SQLAlchemy's Model.query."""
    __tablename__ = 'saved_searches'

    id         = db.Column(db.Integer, primary_key=True)
    query_text = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Alert(db.Model):
    __tablename__ = 'alerts'

    id           = db.Column(db.Integer, primary_key=True)
    alert_type   = db.Column(db.String(64), nullable=False, index=True)
    severity     = db.Column(db.String(16), nullable=False)   # ERROR, CRIT, WARN, INFO
    title        = db.Column(db.String(255), nullable=False)
    body         = db.Column(db.Text)
    source       = db.Column(db.String(255))   # logger name for log-based alerts
    recording_id = db.Column(db.Integer, db.ForeignKey('recordings.id'), nullable=True)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    read_at      = db.Column(db.DateTime)       # null = unread
    dismissed_at = db.Column(db.DateTime)       # null = active


class IgnoredAlertPattern(db.Model):
    """A user-created suppression rule: 'stop surfacing alerts like this one.'

    alert_type + title_pattern (the title with digit runs collapsed to '#') is the match key
    create_alert() checks before writing a new Alert row - see
    app/alerts.py::normalize_alert_title / _record_if_ignored. Exists so a routinely-firing
    alert whose title carries a live count (e.g. "skipped 250 malformed channel URL(s)") can
    be acknowledged once instead of re-dismissed every time the count changes.
    """
    __tablename__ = 'ignored_alert_patterns'
    __table_args__ = (
        db.UniqueConstraint('alert_type', 'title_pattern', name='uq_ignored_alert_pattern'),
    )

    id              = db.Column(db.Integer, primary_key=True)
    # No index=True: uq_ignored_alert_pattern above is (alert_type, title_pattern) - see
    # RedundantPrefixIndexTests.
    alert_type      = db.Column(db.String(64), nullable=False)
    title_pattern   = db.Column(db.String(255), nullable=False)
    example_title   = db.Column(db.String(255))
    created_at      = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    match_count     = db.Column(db.Integer, nullable=False, default=0)
    last_matched_at = db.Column(db.DateTime)


class SchemaMigration(db.Model):
    """Audit log of applied schema migrations (see app/migrations.py). The authoritative
    schema version stamp is PRAGMA user_version, not this table - these rows exist so a
    user's DB records when each step ran and under which app version."""
    __tablename__ = 'schema_migrations'

    id          = db.Column(db.Integer, primary_key=True)
    version     = db.Column(db.Integer, nullable=False, unique=True)
    description = db.Column(db.String(255), nullable=False)
    app_version = db.Column(db.String(32), nullable=False)
    applied_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    duration_ms = db.Column(db.Integer)


class SearchIndexState(db.Model):
    """One row per FTS5 search index, recording whether it is safe to query.

    The indexes themselves are raw SQLite virtual tables with no ORM presence (see
    app/search_index.py); this is the only part of them SQLAlchemy owns. It exists so a
    search can tell "the index is current" from "the last rebuild blew up" - without it,
    a failed rebuild would leave an empty index that silently answers every query with
    zero rows, which is exactly the failure this project's founding principle forbids.
    Consumers ask search_index.search_index_readiness() and fall back to LIKE when it is
    False. dev/changelog/364, dev/changelog/365.
    """
    __tablename__ = 'search_index_state'

    id          = db.Column(db.Integer, primary_key=True)
    # 'channels' | 'programs' - the SEARCH_INDEXES keys in app/search_index.py
    name        = db.Column(db.String(64), nullable=False, unique=True)
    # OK = rebuilt cleanly and safe to query; FAILED = do not query, fall back to LIKE;
    # BUILDING = a chunked rebuild is in flight, so the index is incomplete right now and
    # readers must fall back too (search_index.STATUS_BUILDING has the full reasoning).
    status      = db.Column(db.String(32), nullable=False, default='FAILED')
    rebuilt_at  = db.Column(db.DateTime)
    duration_ms = db.Column(db.Integer)
    row_count   = db.Column(db.Integer)
    error       = db.Column(db.Text)
    # What the source table looked like when this index was built, as text (see
    # search_index._SOURCE_WATERMARK_SQL). Compared against a fresh read on every readiness
    # check, so a rebuild that never ran degrades search to LIKE instead of leaving it
    # matching the previous sync's text. Status alone cannot detect that: it still says OK.
    # dev/changelog/365.
    source_watermark = db.Column(db.Text)


class AccountSyncLog(db.Model):
    __tablename__ = 'account_sync_logs'

    id                 = db.Column(db.Integer, primary_key=True)
    account_id         = db.Column(db.Integer, db.ForeignKey('accounts.id'),
                                   nullable=False, index=True)
    started_at         = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at       = db.Column(db.DateTime)
    # SUCCESS | ERROR | PARTIAL
    status             = db.Column(db.String(32))
    channels_synced    = db.Column(db.Integer, default=0)
    epg_entries_synced = db.Column(db.Integer, default=0)
    error_message      = db.Column(db.Text)
    # NULL on syncs from before dev/changelog/480 - "not tracked", never a false zero
    # (Product Principle 1).
    channels_added     = db.Column(db.Integer)
    channels_removed   = db.Column(db.Integer)
    # Entries the channel upsert skipped: a stream URL with no "://", and a stream_id already
    # seen earlier in the same sync (dev/changelog/926). NULL = not tracked - a sync from
    # before the columns with no alert to recover the count from - never a false zero.
    skipped_malformed_urls       = db.Column(db.Integer)
    skipped_duplicate_stream_ids = db.Column(db.Integer)


# ── Scheduled-job run history (dev/changelog/592) ─────────────────────────────
# Account syncs already have their own per-run log (AccountSyncLog above) - this table is
# for the system jobs that don't: config backup, recording retention, DB maintenance.
# jobs.py::_build_job_list reads both sources to build a job's estimated runtime.

JOB_RUN_SUCCESS = 'SUCCESS'
JOB_RUN_FAILED  = 'FAILED'

# How many of a job's most recent runs to keep and average over. A rolling window, not a
# lifetime average, so a job that got slower/faster recently reflects that quickly.
JOB_RUN_HISTORY_LIMIT = 50


class JobRun(db.Model):
    __tablename__ = 'job_runs'

    id          = db.Column(db.Integer, primary_key=True)
    job_id      = db.Column(db.String(64), nullable=False, index=True)
    started_at  = db.Column(db.DateTime, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=False)
    outcome     = db.Column(db.String(16), nullable=False)  # JOB_RUN_SUCCESS | JOB_RUN_FAILED


def record_job_run(job_id: str, started_at: datetime, finished_at: datetime, outcome: str) -> None:
    """Insert one completed run and prune job_id's history back down to
    JOB_RUN_HISTORY_LIMIT rows. Two separate retry_on_locked closures (CLAUDE.md commit
    rule) - each is independently safe to re-run in full, so a lock-retry on the prune can
    never silently skip re-inserting the run.
    """
    from .db_utils import retry_on_locked

    @retry_on_locked()
    def _insert():
        db.session.add(JobRun(job_id=job_id, started_at=started_at,
                               finished_at=finished_at, outcome=outcome))
        db.session.commit()

    _insert()

    @retry_on_locked()
    def _prune():
        stale_ids = [
            row.id for row in
            JobRun.query.filter_by(job_id=job_id)
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .offset(JOB_RUN_HISTORY_LIMIT).all()
        ]
        if stale_ids:
            JobRun.query.filter(JobRun.id.in_(stale_ids)).delete(synchronize_session=False)
            db.session.commit()

    _prune()


def get_job_duration_estimate(job_id: str):
    """(avg_seconds, run_count) over job_id's stored history (successful runs only - a
    FAILED run's wall-clock time isn't a useful estimate of how long the job takes to
    actually do its work). (None, 0) when there is no completed run yet - the caller must
    render that as an explicit "unknown", never fall back to another job's number
    (CLAUDE.md Product Principle 1 / the approved dev/mockups/29 contract)."""
    rows = (
        JobRun.query.filter_by(job_id=job_id, outcome=JOB_RUN_SUCCESS)
        .order_by(JobRun.started_at.desc(), JobRun.id.desc())
        .limit(JOB_RUN_HISTORY_LIMIT).all()
    )
    if not rows:
        return None, 0
    total = sum((row.finished_at - row.started_at).total_seconds() for row in rows)
    return total / len(rows), len(rows)
