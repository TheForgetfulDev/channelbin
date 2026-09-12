"""
Channel quality testing.

Connects to each in-guide channel for a configurable duration, records a short
clip, then extracts resolution/fps/bitrate/drop metrics and a screenshot.

Run state lives in a single module-level RunState instance (_state), protected
by _lock. Only one test run can be active at a time; is_running covers both the
active test and the wait period between channels.
"""
import collections
import itertools
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from . import admission
from .proc_utils import GrowthMonitor, terminate_or_kill, wait_for_file_data
from .toolchain import ffprobe_missing
from .url_utils import mask_creds, mask_creds_in_text

log = logging.getLogger(__name__)

_lock = threading.Lock()


@dataclass
class RunState:
    """All mutable run state, guarded by _lock. _reset_run_state() swaps in a
    fresh instance at run start so every field (including any added later)
    returns to its default automatically; clear() is the partial end-of-run
    reset that keeps the last-run summary fields for the status UI."""
    is_running: bool = False
    stop_requested: bool = False

    # Which account's connection slot the currently-running test's ffmpeg process is
    # holding, and the process itself - lets recorder.py find and kill it to preempt
    # a test when a recording needs that account's connection slot (see
    # kill_active_test_for_account() below and app/connection_limits.py).
    active_test_proc: Optional[subprocess.Popen] = None
    active_test_account_id: Optional[int] = None
    preempted_by_recording: bool = False

    current_channel_id: Optional[int] = None
    current_channel_name: str = ''
    total_channels: int = 0
    completed_channels: int = 0
    run_started_at: Optional[datetime] = None
    last_skip_reason: Optional[str] = None
    current_job_id: Optional[int] = None   # set while running an on-demand job

    # One flag, one meaning (CLAUDE.md): which kind of run this is. 'job' covers both
    # system and custom OnDemandTestJob runs (current_job_id set or None respectively);
    # 'pre_check' is a DESIGN-prerecord-checks.md §3 run, where current_job_id is always
    # None and pre_check_recording_id names the recording being protected instead. Every
    # render/branch site of run state must name both kinds explicitly, never infer the
    # kind from current_job_id being None/set.
    run_kind: str = 'job'
    pre_check_recording_id: Optional[int] = None

    # Live visibility state
    log_entries: collections.deque = field(default_factory=lambda: collections.deque(maxlen=500))
    current_phase: str = 'idle'            # 'idle' | 'testing' | 'waiting'
    current_test_started_at: Optional[datetime] = None
    current_test_drop_count: int = 0
    current_test_screenshot_path: Optional[str] = None
    current_channel_url: Optional[str] = None
    current_live_bytes: int = 0
    current_connect_attempt: int = 0
    max_connect_attempts: int = 1
    wait_started_at: Optional[datetime] = None
    wait_duration_seconds: Optional[float] = None

    # Between-channel context (populated during 'waiting' phase)
    last_channel_name: str = ''
    last_channel_id: Optional[int] = None
    next_channel_name: str = ''
    next_channel_id: Optional[int] = None

    # The app/admission.py ticket this run holds, acquired in _reset_run_state and given
    # back in _end_run. Deliberately NOT touched by clear(): _end_run reads it out first,
    # and a clear() that dropped it would leak the ticket instead of releasing it.
    admission_ticket: object = None

    def clear(self):
        """Partial end-of-run reset: back to idle, but keep the last-run summary
        (totals, run_started_at, last_skip_reason, last screenshot/drop count,
        log_entries) so the status UI can still show the finished run.
        Must be called with _lock held."""
        self.is_running = False
        self.current_phase = 'idle'
        self.current_channel_id = None
        self.current_channel_name = ''
        self.current_channel_url = None
        self.current_live_bytes = 0
        self.current_connect_attempt = 0
        self.wait_started_at = None
        self.wait_duration_seconds = None
        self.last_channel_name = ''
        self.last_channel_id = None
        self.next_channel_name = ''
        self.next_channel_id = None
        self.current_job_id = None
        self.run_kind = 'job'
        self.pre_check_recording_id = None


_state = RunState()

# Monotonic across the process lifetime - see _append_log for why it must never reset.
_log_seq = itertools.count(1)


# ── Public API ────────────────────────────────────────────────────────────────

def _append_log(level: str, message: str):
    """Append a timestamped log entry to the in-memory ring buffer.

    Lock-free by design (deque.append and next() on an itertools.count are both
    atomic) - callers invoke it both with and without _lock held, and _lock is not
    reentrant. Don't replace _log_seq with a plain `+= 1` counter; that read-modify-write
    is not atomic and would break that contract.

    'seq' lets the UI render the log append-only instead of rebuilding the whole node
    (which destroys any text the user has highlighted). It must never reset: the deque
    evicts from the front once maxlen is hit, so a positional diff would silently skip or
    duplicate lines, and a per-run reset would let a client re-append a previous run's
    entries."""
    _state.log_entries.append({
        'ts': datetime.utcnow().isoformat(),
        'level': level,
        'msg': message,
        'seq': next(_log_seq),
    })


def get_status() -> dict:
    with _lock:
        s = _state
        screenshot_url = None
        if s.current_test_screenshot_path:
            filename = os.path.basename(s.current_test_screenshot_path)
            screenshot_url = f'/channel-tests/screenshots/{filename}'
        return {
            'is_running': s.is_running,
            'stop_requested': s.stop_requested,
            'current_channel_id': s.current_channel_id,
            'current_channel_name': s.current_channel_name,
            'current_channel_url': mask_creds(s.current_channel_url),
            'current_phase': s.current_phase,
            'current_test_started_at': s.current_test_started_at.isoformat() if s.current_test_started_at else None,
            'current_test_drop_count': s.current_test_drop_count,
            'current_test_screenshot_url': screenshot_url,
            'current_live_bytes': s.current_live_bytes,
            'current_connect_attempt': s.current_connect_attempt,
            'max_connect_attempts': s.max_connect_attempts,
            'wait_started_at': s.wait_started_at.isoformat() if s.wait_started_at else None,
            'wait_duration_seconds': s.wait_duration_seconds,
            'last_channel_name': s.last_channel_name,
            'last_channel_id': s.last_channel_id,
            'next_channel_name': s.next_channel_name,
            'next_channel_id': s.next_channel_id,
            'total_channels': s.total_channels,
            'completed_channels': s.completed_channels,
            'run_started_at': s.run_started_at.isoformat() if s.run_started_at else None,
            'last_skip_reason': s.last_skip_reason,
            'current_job_id': s.current_job_id,
            'run_kind': s.run_kind,
            'pre_check_recording_id': s.pre_check_recording_id,
            'logs': list(s.log_entries),
        }


def is_running() -> bool:
    """Whether a test run is active. The cheap cross-module read - other actors
    (the sync job) must use this rather than reaching into _state, and rather than
    get_status(), which rebuilds the whole status dict including the log ring buffer."""
    with _lock:
        return _state.is_running


def request_stop():
    with _lock:
        _state.stop_requested = True
    log.info('Channel test stop requested')


def kill_active_test_for_account(app, account_id: int) -> bool:
    """Preempt the currently-running test IF it belongs to account_id. Called by
    recorder.py when a recording needs the connection slot the test holds -
    recordings always win over tests. Returns True if the test was preempted.

    The flag is set on an account match even when no ffmpeg process is registered
    yet: the test's account is registered at slot-acquire time, so a match with
    proc=None means the tester is inside the acquire→Popen window (or a
    connect-retry sleep). Returning False there would let it launch ffmpeg after
    its slot was already stripped - two provider connections on a one-slot
    account. The tester re-reads this flag at every connect-loop boundary and
    aborts itself; see _run_channel_test_inner. Do not restore the old
    `active_test_proc is None` early return."""
    with _lock:
        if _state.active_test_account_id != account_id:
            return False
        proc = _state.active_test_proc
        _state.preempted_by_recording = True
    log.warning('Channel test on account %d preempted by a starting recording', account_id)
    _append_log('WARN', 'Test interrupted - a recording is starting and needs this connection slot')
    if proc is not None:
        terminate_or_kill(proc)
    return True


def _reset_run_state(job_id=None, run_kind='job', pre_check_recording_id=None, label=''):
    """Start-of-run reset: swap in a fresh RunState so every field returns to
    its default. Must be called with _lock held.

    Also registers the run on the database-contention axis (app/admission.py) in the same
    critical section that sets is_running, so no other actor can ever observe one without
    the other - that gap is precisely the check-then-act race the registry closes
    (dev/changelog/679). The tester is refused by nothing, so this always grants; the
    ticket is given back by _end_run(), which every path that ends a run must call."""
    global _state
    _state = RunState(
        is_running=True,
        current_phase='testing',
        run_started_at=datetime.utcnow(),
        current_job_id=job_id,
        run_kind=run_kind,
        pre_check_recording_id=pre_check_recording_id,
    )
    _state.admission_ticket = admission.try_start(admission.KIND_TESTER, label)


def _end_run():
    """End-of-run teardown: clear the run state and give the admission ticket back.

    Every path that ends a run goes through this rather than calling _state.clear()
    directly - the ticket is acquired alongside is_running, so it has to be released
    wherever that state is torn down, or the kinds that yield to the tester defer forever.
    Must be called WITHOUT _lock held (release() logs, and logging is I/O)."""
    with _lock:
        ticket = _state.admission_ticket
        _state.admission_ticket = None
        _state.clear()
    admission.release(ticket)


def _run_channel_loop(app, channels, wait_sec, job_id=None):
    """Inner loop: iterate through channels, calling run_channel_test for each.

    Called by run_on_demand_test_job for custom and system jobs alike.
    """
    n = len(channels)
    with _lock:
        _state.total_channels = n

    _append_log('INFO', f'Starting run - {n} channel{"s" if n != 1 else ""} to test')
    log.info('Starting channel test run: %d channels', n)

    for i, ch in enumerate(channels):
        with _lock:
            if _state.stop_requested:
                log.info('Channel test run cancelled after %d/%d channels', i, n)
                _append_log('WARN', f'Run stopped by user after {i}/{n} channels')
                return False  # cancelled

            _state.current_phase = 'testing'
            _state.current_channel_id = ch.id
            _state.current_channel_name = ch.name
            _state.current_channel_url = ch.stream_url
            _state.current_test_started_at = None
            _state.current_test_drop_count = 0
            _state.current_test_screenshot_path = None
            _state.current_live_bytes = 0
            _state.current_connect_attempt = 0

        log.info('Testing channel %d/%d: %s (id=%d)', i + 1, n, ch.name, ch.id)
        _append_log('INFO', f'[{i + 1}/{n}] Testing: {ch.name}')
        _append_log('INFO', f'URL: {mask_creds(ch.stream_url)}')

        # defer_group_format: a run settles its groups' formats once at the end, over a
        # complete test map - see _settle_group_formats (dev/changelog/934).
        run_channel_test(app, ch.id, job_id=job_id, defer_group_format=True)

        with _lock:
            _state.completed_channels = i + 1
            stopped = _state.stop_requested

        if stopped:
            _append_log('WARN', f'Run stopped by user after {i + 1}/{n} channels')
            return False  # cancelled

        if i < n - 1:
            next_ch = channels[i + 1]
            with _lock:
                _state.current_phase = 'waiting'
                _state.current_channel_id = None
                _state.last_channel_name = ch.name
                _state.last_channel_id = ch.id
                _state.next_channel_name = next_ch.name
                _state.next_channel_id = next_ch.id
                _state.current_channel_name = ''
                _state.current_channel_url = ''
                _state.current_test_screenshot_path = None
                _state.current_test_drop_count = 0
                _state.current_live_bytes = 0
                _state.wait_started_at = datetime.utcnow()
                _state.wait_duration_seconds = float(wait_sec)
            _append_log('INFO', f'Waiting {wait_sec}s before next channel…')
            _interruptible_sleep(wait_sec)
            with _lock:
                _state.wait_started_at = None
                _state.wait_duration_seconds = None

    with _lock:
        done = _state.completed_channels
    log.info('Channel test run finished: %d/%d channels tested', done, n)
    _append_log('INFO', f'Run complete - {done}/{n} channels tested')
    return True  # completed normally


# (HealthCheckProfile attribute, channel_testing.* config key, hardcoded default).
# One list so resolve_health_check_settings() and health_check_profile_payload() can
# never disagree about which fields a profile overrides - the payload's `from_default`
# is exactly the inverse of the resolver's per-field pick.
_HEALTH_CHECK_SETTINGS = (
    ('test_duration_seconds',         'test_duration_seconds',         120),
    ('wait_between_channels_seconds', 'wait_between_channels_seconds', 180),
    ('screenshots_enabled',           'screenshots_enabled',           True),
    ('connect_retries',               'connect_retries',               2),
    ('connect_timeout_seconds',       'connect_timeout_seconds',       15),
    ('connect_retry_delay_seconds',   'connect_retry_delay_seconds',   10),
)


def resolve_health_check_settings(ct_cfg: dict, profile) -> dict:
    """Effective channel-test execution settings: a HealthCheckProfile field
    overrides the channel_testing.* config default when set, per field."""
    def pick(field, key, default):
        if profile is not None:
            val = getattr(profile, field)
            if val is not None:
                return val
        return ct_cfg.get(key, default)
    return {field: pick(field, key, default)
            for field, key, default in _HEALTH_CHECK_SETTINGS}


# Every threshold below is expressed against the duration the test actually asked for.
# A test captures `-t <duration>` of content, so "the stream died early" can only ever be
# a statement about the shortfall against THAT number - an absolute floor silently inverts
# into "every short profile fails" the moment someone configures a duration below it
# (dev/docs/BUGS.md 2026-08-28: a 5s profile failed every channel it touched, message and
# all: "only 5s (expected 5s)").
_HARD_FAIL_CEILING_SECONDS = 10.0   # a capture shorter than this always failed; now a cap
_HARD_FAIL_FRACTION = 0.5
_WARN_FRACTION = 0.8
# Slack for mux/rounding noise. A stream copy stops at the first packet PAST -t, so a healthy
# capture lands slightly over; at a 1s duration the 80% warn band sits 0.2s away from the
# requested length, close enough that container rounding alone could spend a -10 warn penalty.
# A shortfall under half a second is not a finding at any duration.
_SHORT_GRACE_SECONDS = 0.5


def empty_probe_warning() -> str:
    """The WARN line for a test whose probe reported no video stream.

    A test log is the only surface that explains that test's numbers, so it must not
    report ChannelBin's own missing binary as a fault in the channel. parse_ffprobe()
    returns {} for both, and this test used to render the stream verdict unconditionally
    (dev/changelog/911). Every measured field is None in the missing-ffprobe state and
    none of them is evidence about the feed, which is why the two lines say opposite
    things about what was learned rather than differing in wording.
    """
    if ffprobe_missing():
        return ('ffprobe is not installed, so this test could not inspect the stream - '
                'no resolution, frame or track data was measured. See Maintenance > '
                'External tools.')
    return 'ffprobe found no video stream in recording'


def short_capture_verdict(actual: float, requested: float):
    """How badly a test's captured clip fell short of the duration it asked for.

    Returns (verdict, message) where verdict is 'fail' (the stream died with too little
    to say anything about), 'warn' (short but usable), or None (fine).

    The hard-fail threshold is min(10s, half the requested duration): unchanged at every
    duration of 20s or more, and scaling down from there so a deliberately short profile
    is judged against what it asked for.
    """
    if not requested or requested <= 0 or actual is None:
        return None, None
    fail_threshold = min(_HARD_FAIL_CEILING_SECONDS, requested * _HARD_FAIL_FRACTION)
    if actual < fail_threshold:
        return 'fail', (f'Stream ended early - captured only {actual:.1f}s '
                        f'of the {requested:.0f}s requested')
    if actual < requested * _WARN_FRACTION and (requested - actual) > _SHORT_GRACE_SECONDS:
        return 'warn', (f'Short recording: only {actual:.1f}s of the '
                        f'{requested:.0f}s requested')
    return None, None


def health_check_profile_payload(ct_cfg: dict, profiles) -> dict:
    """Everything the Create-health-check modal's "What this will do" readout renders,
    for every profile at once: each profile's *effective* settings plus the list of
    fields that fell back to the global default (which is what earns the grey `default`
    pill in the UI).

    `ct_cfg` is the already-loaded `channel_testing` config dict and `profiles` the
    already-queried HealthCheckProfile list - this is built ONCE per request and handed
    to the template, never per group row (CLAUDE.md no-hidden-I/O-in-loops). Deliberately
    has no "loads config if omitted" argument for the same reason.

    A profile field set to a falsy-but-real value (screenshots_enabled=False) is a
    profile value, not an unset one: `from_default` tests `is None`, never truthiness.
    """
    entries = [{
        'id': None,
        'name': 'Global defaults (no profile)',
        'settings': resolve_health_check_settings(ct_cfg, None),
        'from_default': [field for field, _key, _d in _HEALTH_CHECK_SETTINGS],
    }]
    for p in profiles:
        entries.append({
            'id': p.id,
            'name': p.name,
            'settings': resolve_health_check_settings(ct_cfg, p),
            'from_default': [field for field, _key, _d in _HEALTH_CHECK_SETTINGS
                             if getattr(p, field) is None],
        })
    return {'defaults': resolve_health_check_settings(ct_cfg, None), 'profiles': entries}


def monitored_channel_ids():
    """Set of channel IDs covered by at least one *active recurring* health-check job -
    i.e. channels whose resolution/FPS drift will be re-checked on an ongoing basis.

    "Active recurring" is channel_groups.active_recurring_jobs()' definition, read from
    there rather than restated. Effective set per job:
      - system 'TV Guide Channels' job: **one channel per guide row, plus one per group
        with no schedule of its own** - a standalone in-guide channel, or the member
        currently serving a group's row (channel_groups.system_check_targets,
        dev/changelog/752). A group's non-serving members are deliberately NOT covered:
        they go stale unless that group carries a schedule of its own, which is the
        accepted division of labor (DESIGN-channel-groups-model.md 6). This is also why
        a guide group with no schedule stops reporting its serving member as unmonitored.
      - custom job: its group's members that a run would actually test
        (channel_groups.check_run_channels - the memberships whose test_enabled is on,
        minus any whose channel-wide Channel.test_enabled is off).

    Single batched computation - call once and membership-test against the returned set.
    Must be called inside an app context."""
    from .channel_groups import check_run_channels, active_recurring_jobs

    monitored: set[int] = set()
    for job in active_recurring_jobs():
        if job.group is None:
            continue
        monitored.update(ch.id for ch in check_run_channels(job.group))
    return monitored


def imminent_recording_conflict() -> Optional[str]:
    """A recording starting soon that argues against starting a health-check run right now.

    None = no conflict. Mirrors sync's skip_sync_if_recording_within_minutes guard
    (DESIGN-concurrency.md 5.5) - shared by run_on_demand_test_job's own skip-with-alert
    path (scheduled fires, applies to system and custom jobs alike) and the manual Run Now
    routes' warn+force pre-check (same shape as accounts.sync_conflicts, 5.4).
    """
    # Re-imported locally so tests can patch app.config.load_config (CLAUDE.md Testing).
    from .config import load_config
    from .database import Recording, REC_STATUS_SCHEDULED

    within_minutes = load_config().get('channel_testing', {}).get('skip_if_recording_within_minutes', 10)
    if not within_minutes or within_minutes <= 0:
        return None

    cutoff = datetime.utcnow() + timedelta(minutes=within_minutes)
    upcoming = Recording.query.filter(
        Recording.status == REC_STATUS_SCHEDULED,
        Recording.start_time <= cutoff,
    ).order_by(Recording.start_time).first()
    if upcoming is None:
        return None
    return (f'A recording starts within {within_minutes} minutes ("{upcoming.name}") - '
            'starting a health check run now may compete for the connection limit.')


def _record_skipped_for_busy_tester(app, job_id: int, running_kind: str,
                                    running_job_id: Optional[int],
                                    running_pre_check_recording_id: Optional[int]):
    """Record a scheduled fire dropped because the tester was already busy with something
    else, on the run log and last-skip reason every other skip path here already writes to
    (dev/changelog/928). Called with _lock released - do the DB work below outside _lock,
    never under it (get_status()/other lock users would deadlock); the state write and log
    append take _lock themselves, briefly, once that work is done.

    running_kind enumerates every RunState.run_kind value explicitly (CLAUDE.md "states
    are enumerated") so the reason always names the real thing holding the slot, not just
    "another health check" - a scheduled job can just as easily collide with a
    pre-recording check or a manual one-off test as with another job.
    """
    with app.app_context():
        from . import db
        from .database import OnDemandTestJob, Recording

        skipped_job = db.session.get(OnDemandTestJob, job_id)
        skipped_name = skipped_job.name if skipped_job is not None else f'job {job_id}'

        if running_kind == 'pre_check':
            rec = db.session.get(Recording, running_pre_check_recording_id or 0)
            running_desc = (f'A pre-recording check for "{rec.name}"' if rec is not None
                             else 'A pre-recording check')
        elif running_kind == 'one_off':
            running_desc = 'A manual channel test'
        elif running_job_id is not None:
            running_job = db.session.get(OnDemandTestJob, running_job_id)
            running_desc = (f'Health check "{running_job.name}"' if running_job is not None
                             else 'Another health check')
        else:
            running_desc = 'Another health check'

    # The run log only. `_state.last_skip_reason` and `current_job_id` describe the run that
    # is currently going, and this skip belongs to a different job - writing them here would
    # give one field two meanings and overwrite the live run's own state, which is why the
    # log line names the skipped job explicitly instead.
    _append_log('WARN', f'Run skipped for "{skipped_name}" - '
                        f'{running_desc} is already running')


def _revert_job_status_after_busy_skip(app, job_id: int):
    """Put a job row back to a resting status after its run was skipped for a busy tester.

    Every manual entry point (start / resume / test-selected / restart / create) goes through
    routes/channel_tests.py::_start_job_run, which commits status='RUNNING' and only then spawns
    the thread. If that thread then loses the race for the tester, it returns from the busy
    branch without ever reaching the try/finally that saves a final status, and the row stays
    RUNNING with nothing behind it - recoverable only through the manual force-cancel route.

    Reverting only a row that still reads RUNNING is what keeps a scheduled fire (whose row was
    never set RUNNING) untouched. The caller additionally must not call this for the job that is
    itself the busy run - see the call site.
    """
    with app.app_context():
        from . import db
        from .database import OnDemandTestJob
        from .db_utils import retry_on_locked
        from .scheduler import finalize_on_demand_job_status

        @retry_on_locked()
        def _revert_status_and_commit():
            job = db.session.get(OnDemandTestJob, job_id)
            if job is None or job.status != 'RUNNING':
                return None
            kind = finalize_on_demand_job_status(job, False)
            new_status = job.status
            db.session.commit()
            return kind, new_status

        result = _revert_status_and_commit()

    if result is not None:
        kind, new_status = result
        log.warning('On-demand job %d was marked RUNNING by its manual start but the tester was '
                    'already busy - reverted to %s (%s schedule)', job_id, new_status, kind)


def run_on_demand_test_job(app, job_id: int, channel_id_subset: Optional[List[int]] = None,
                            force: bool = False):
    """Test the channels of the job's attached group (channel_groups.check_run_channels:
    stored membership in position order, or the live in-guide+test_enabled set for the
    system 'TV Guide Channels' group, which also honors
    channel_testing.skip_if_recording_active).

    Called from a background thread. Only one run can be active at a time.
    channel_id_subset: if provided, only test channels whose IDs appear in this list
    (in the original channel order). Used by resume/restart routes.
    force: skips the imminent_recording_conflict() check below (DESIGN-concurrency.md 5.5).
    Manual routes set this after the user has already been warned and opted to proceed
    (same warn+force shape as accounts.sync_conflicts, 5.4); a scheduled fire never passes
    it, so the check always applies there. NOTE: run_pre_check (future "Pre-record checks
    C", DESIGN-prerecord-checks.md §6) intentionally bypasses this guard entirely - it is
    not read here, so no change is needed on this end when that lands.

    Uses two short-lived app contexts (setup + teardown) with NO outer context
    wrapping _run_channel_loop. This avoids a SQLite "database is locked" error
    caused by nested app context teardowns (db.session.remove()) pulling out the
    connection that the outer context's session was using mid-loop.
    """
    with _lock:
        busy = _state.is_running
        if busy:
            running_kind = _state.run_kind
            running_job_id = _state.current_job_id
            running_pre_check_recording_id = _state.pre_check_recording_id
        else:
            _reset_run_state(job_id=job_id, label=f'health check job {job_id}')

    if busy:
        log.info('Channel test run already in progress - skipping on-demand job %d', job_id)
        _record_skipped_for_busy_tester(app, job_id, running_kind, running_job_id,
                                         running_pre_check_recording_id)
        # Never for the job that IS the busy run: a recurring job's own trigger firing again
        # mid-run lands here with running_job_id == job_id, and reverting then would take the
        # live run's row out of RUNNING while it is still testing channels.
        if running_job_id != job_id:
            _revert_job_status_after_busy_skip(app, job_id)
        return

    channels = []
    wait_sec = 180
    completed = False
    skipped = False

    try:
        # ── Phase 1: load data, set RUNNING (short-lived context) ─────────────
        with app.app_context():
            from . import db
            from .config import load_config
            from .database import OnDemandTestJob, Recording, REC_STATUS_IN_PROGRESS
            from .channel_groups import check_run_channels

            from .db_utils import retry_on_locked

            job = db.session.get(OnDemandTestJob, job_id)
            if job is None:
                log.error('run_on_demand_test_job: job %d not found', job_id)
                # Self-heal: whatever registered this run (almost always a stray recurring
                # CronTrigger left behind after the DB row is gone - dev/docs/BUGS.md
                # 2026-08-10) would otherwise keep firing this same error forever.
                from .scheduler import remove_job_if_exists
                remove_job_if_exists(f'od_job_{job_id}')
                skipped = True
                return
            is_system = job.is_system

            cfg = load_config()
            ct_cfg = cfg.get('channel_testing', {})

            active_rec = None
            if is_system and ct_cfg.get('skip_if_recording_active', True):
                active_rec = Recording.query.filter_by(status=REC_STATUS_IN_PROGRESS).first()
            if active_rec is not None:
                msg = 'A recording is currently in progress'
                log.info('Skipping TV Guide Channels run - %s', msg)
                with _lock:
                    _state.last_skip_reason = msg
                _append_log('WARN', f'Run skipped - {msg}')
                skipped = True
                return

            if not force:
                conflict_reason = imminent_recording_conflict()
                if conflict_reason:
                    log.info('Skipping on-demand job %d - %s', job_id, conflict_reason)
                    with _lock:
                        _state.last_skip_reason = conflict_reason
                    _append_log('WARN', f'Run skipped - {conflict_reason}')
                    skipped = True
                    return

            @retry_on_locked()
            def _mark_job_running_and_commit():
                j = db.session.get(OnDemandTestJob, job_id)
                if j is not None:
                    j.status = 'RUNNING'
                    db.session.commit()
                return j

            job = _mark_job_running_and_commit()

            wait_sec = resolve_health_check_settings(ct_cfg, job.profile)['wait_between_channels_seconds']

            if job.group is None:
                log.error('run_on_demand_test_job: job %d has no attached group', job_id)
                candidates = []
            else:
                candidates = check_run_channels(job.group)
            subset_set = set(channel_id_subset) if channel_id_subset is not None else None
            channels = [ch for ch in candidates
                        if subset_set is None or ch.id in subset_set]
        # Phase 1 context closed - connection released; channel objects are now
        # detached but their column attributes (id, name, stream_url) are intact.

        if not channels:
            log.info('On-demand job %d: no valid channels found', job_id)
            if is_system:
                _append_log('WARN', 'No testable guide channels found - check Guide and Test Enabled flags')
            else:
                _append_log('WARN', 'No valid channels found for this job')
        else:
            # ── Phase 2: run tests - no outer app context ──────────────────────
            # run_channel_test() opens its own short-lived context per channel,
            # so there are no nested/concurrent sessions to cause lock conflicts.
            completed = _run_channel_loop(app, channels, wait_sec, job_id=job_id)

    finally:
        # ── Phase 3: save final status + clear module state ───────────────────
        # A skipped run never left SCHEDULED (and never ran), so there is no final
        # status to record - just clear the module state.
        try:
            if not skipped:
                _save_job_final_status(app, job_id, completed)
        except Exception:
            log.exception('run_on_demand_test_job: failed to save final status for job %d', job_id)
        # In the `finally` on purpose: a run stopped, cancelled or aborted part-way still
        # settles its groups from whatever it did measure, rather than leaving them on the
        # half-updated state the run created.
        if not skipped and channels:
            _settle_group_formats(app, [ch.id for ch in channels])
        _end_run()


def _settle_group_formats(app, tested_channel_ids):
    """Settle the format state of every group this run gathered data for, once, when the
    run is over - the trigger half of DECIDED 9 (dev/changelog/753), plus the reconcile
    pass that used to run after every single test (dev/changelog/934).

    **Keyed on the channels actually tested, not on the job's own group.** The automatic
    "TV Guide Channels" job probes one member of each guide row and one member of each
    scheduleless group (dev/changelog/752), so its `job.group` is the system group, which
    has no strategy of its own - reading that instead would starve exactly the groups the
    fallback exists to serve.

    **Once per run, over a complete test map.** Mid-run half a group's members carry this
    run's numbers and the rest carry the previous run's, so the ranking that picks the
    format - the strategy's bucket, or the derived reference under highest_score - keeps
    changing and settles only by accident. Measured on the live database: Fox Sports 1's
    reference moved to 1080p60 and back to 720p60 inside 18 minutes on 2026-09-11, and
    CW's lock moved 1080p60 to 720p30 and back within 15 minutes on 09-04, each move
    changing which member a recording would start on. A format that follows the data is
    the point; one that changes several times while the data is still arriving is not.

    Both halves per group, because apply_format_strategy() reconciles only when it
    actually moved or cleared the lock: a strategy that manages no lock (highest_score,
    unmanaged) would otherwise never reconcile at all now that the per-test pass is gone.

    Best-effort per group - a failure on one must not abort the others or the run's
    teardown."""
    if not tested_channel_ids:
        return
    with app.app_context():
        from .config import load_config
        from .database import ChannelGroupMember
        from .channel_groups import (apply_format_strategy, evaluate_and_reconcile_group,
                                     DEFAULT_FAILING_STREAK_THRESHOLD)
        try:
            # Hoisted once for the whole loop (CLAUDE.md no-hidden-I/O-in-per-row-loops).
            streak_threshold = load_config().get('channel_testing', {}).get(
                'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
            groups = {m.group for m in ChannelGroupMember.query.filter(
                ChannelGroupMember.channel_id.in_(list(tested_channel_ids))).all()
                if m.group is not None}
        except Exception:
            log.exception('format strategy: could not resolve the groups for this run')
            return
        for group in groups:
            try:
                plan = apply_format_strategy(group, streak_threshold)
                # apply_format_strategy() reconciles for itself when it moved or cleared
                # the lock; every other case still needs the pass that logs which members
                # now differ from the group's format.
                if not (plan or {}).get('moved'):
                    evaluate_and_reconcile_group(group, streak_threshold)
            except Exception:
                log.exception('format settle failed for group %s',
                              getattr(group, 'id', '?'))


def _save_job_final_status(app, job_id: int, completed: bool):
    with app.app_context():
        from . import db
        from .database import OnDemandTestJob
        from .db_utils import retry_on_locked
        from .scheduler import finalize_on_demand_job_status, cancel_on_demand_job_schedule

        # The scheduler next-run lookups inside are idempotent reads, safe to re-run.
        @retry_on_locked()
        def _save_final_status_and_commit():
            job = db.session.get(OnDemandTestJob, job_id)
            if job is None:
                return None
            kind = finalize_on_demand_job_status(job, completed)
            if kind == 'recurring':
                is_window_job = job.recur_use_window
                db.session.commit()
                return ('recurring', is_window_job)
            if kind == 'kept':
                db.session.commit()
                return None
            # 'finished' - a genuinely-finished one-off run; a recurring job or a kept
            # schedule returns above and stays silent. A finished run announces nothing:
            # the job row it just committed carries the outcome and the time it finished,
            # and the run log carries what was tested (dev/changelog/928).
            cancel_on_demand_job_schedule(job)
            db.session.commit()
            return ('finished', None)

        result = _save_final_status_and_commit()

        if result is None:
            return
        kind, payload = result

        if kind == 'recurring':
            is_window_job = payload
            if is_window_job:
                # Outside the retry closure - a non-idempotent side effect (thread spawn)
                # must never live inside a decorated commit unit. Kicks the next window
                # job immediately instead of waiting up to dispatch_interval_minutes.
                import threading
                from .check_window import dispatch_tick
                threading.Thread(
                    target=dispatch_tick, args=(app,), daemon=True,
                    name='check-window-dispatch-kick',
                ).start()
            return


# Covers probe/screenshot/finalize time after the connect loop in the pre-check margin
# guard's worst-case formula (DESIGN-prerecord-checks.md §3) - a module constant since it's
# not something worth exposing as a config knob.
_PRE_CHECK_OVERHEAD_SECONDS = 60


def _pre_check_skip(app, recording_id: int, rec_name: Optional[str], reason: str):
    """Log PRE_CHECK_SKIPPED on the recording naming why - every skip path in run_pre_check
    funnels through here so a silently-skipped pre-check is never indistinguishable from a
    passed one (DESIGN-prerecord-checks.md §4).

    The event on the recording is the whole surface: a skipped pre-check is a fact about
    that recording, and its detail page is where somebody asking "was this checked first"
    already looks (dev/changelog/928). `rec_name` is kept for the log line below, which is
    the only place a recording is identified by name rather than by row.
    """
    from . import db
    from .database import add_recording_event, PRE_CHECK_SKIPPED
    from .db_utils import retry_on_locked

    with app.app_context():
        @retry_on_locked()
        def _commit():
            add_recording_event(recording_id, PRE_CHECK_SKIPPED, detail=reason)
            db.session.commit()
        _commit()

    log.info('run_pre_check: recording %s (%d) skipped - %s',
             rec_name or '?', recording_id, reason)


def run_pre_check(app, recording_id: int):
    """Pre-recording health check (DESIGN-prerecord-checks.md §3): test a recording's
    channel - or, for a group-backed recording, whichever member record-start would pick
    right now - a configurable lead time before the recording starts, so a dead channel
    surfaces before the recording is committed to it.

    Runs the normal single-channel path (run_channel_test), never
    run_on_demand_test_job - so it is not subject to that function's
    imminent_recording_conflict() guard (see that function's docstring for the sanctioned
    structural exception, §6). Called from scheduler.py's precheck_<recording_id>
    DateTrigger job, synchronously in the scheduler callback thread (same as
    _start_job/_stop_job).

    Uses three short-lived app contexts (load+guards / run / outcome), with NO outer
    context wrapping the run_channel_test call - same shape as run_on_demand_test_job,
    for the same reason (nested app-context teardown mid-test can pull the connection out
    from under an outer context holding a session open across the whole test duration).
    """
    from .database import Recording, ChannelTest, Channel, add_recording_event
    from .database import PRE_CHECK_PASSED, PRE_CHECK_FAILED, REC_STATUS_SCHEDULED
    from .db_utils import retry_on_locked

    channel_id = None
    skip_reason = None
    rec_name = None

    # ── Phase 1: load recording + run every guard (short-lived context) ────────
    with app.app_context():
        from . import db
        from .config import load_config

        rec = db.session.get(Recording, recording_id)
        if rec is None or rec.status != REC_STATUS_SCHEDULED:
            log.info('run_pre_check: recording %d missing or not SCHEDULED, skipping', recording_id)
            return
        rec_name = rec.name

        cfg = load_config()
        ct_cfg = cfg.get('channel_testing', {})
        pc_cfg = ct_cfg.get('pre_check', {})

        # Step 2: enablement - RecordingProfile.pre_check_enabled overrides the global
        # flag when set (tri-state: None = inherit).
        enabled = pc_cfg.get('enabled', False)
        if rec.profile is not None and rec.profile.pre_check_enabled is not None:
            enabled = rec.profile.pre_check_enabled
        if not enabled:
            log.debug('run_pre_check: disabled for recording %d, no-op', recording_id)
            return

        # Step 3: margin guard - must provably vacate the connection slot before
        # rec.start_time. Worst-case test duration from the resolved global settings
        # (a pre-check has no HealthCheckProfile of its own).
        connect_retries = ct_cfg.get('connect_retries', 2)
        connect_timeout = ct_cfg.get('connect_timeout_seconds', 15)
        retry_delay = ct_cfg.get('connect_retry_delay_seconds', 10)
        test_duration = ct_cfg.get('test_duration_seconds', 120)
        min_margin = pc_cfg.get('min_margin_seconds', 60)
        retry_minutes = pc_cfg.get('retry_minutes', 5)
        worst_case = ((1 + connect_retries) * connect_timeout
                      + connect_retries * retry_delay
                      + test_duration + _PRE_CHECK_OVERHEAD_SECONDS)

        now = datetime.utcnow()
        margin = (rec.start_time - now).total_seconds()
        if margin < worst_case + min_margin:
            skip_reason = (f'Not enough time before recording start for a pre-check '
                           f'(need ~{int(worst_case + min_margin)}s, have {int(max(margin, 0))}s)')
        else:
            # Step 4: single-run-globally check.
            with _lock:
                busy = _state.is_running
                if not busy:
                    _reset_run_state(run_kind='pre_check', pre_check_recording_id=recording_id,
                                     label=f'pre-check for recording {recording_id}')
            if busy:
                retry_at = now + timedelta(minutes=retry_minutes) if retry_minutes else None
                retry_ok = (retry_at is not None and
                           (rec.start_time - retry_at).total_seconds() >= worst_case + min_margin)
                if retry_ok:
                    from .scheduler import reschedule_precheck
                    reschedule_precheck(recording_id, retry_at)
                    log.info('run_pre_check: recording %d - tester busy, retrying at %s',
                             recording_id, retry_at)
                    return
                skip_reason = ('Tester busy with another run and no time left for a retry '
                               'before recording start' if retry_minutes else
                               'Tester busy with another run (retries disabled)')
            else:
                # Step 6: resolve the channel to test - direct, or (group-backed) the
                # member record-start would pick right now, honoring the busy-member
                # skip rule (app/recorder.py::start_recording mirrors this exact shape).
                #
                # Guarded because the run is already registered at this point: a raise here
                # would carry is_running=True and the admission ticket out of the function
                # with no teardown, wedging the tester and everything that yields to it
                # (dev/changelog/679).
                try:
                    if rec.group_id is not None and rec.group is not None:
                        from .channel_groups import (recording_members, pick_best_member,
                                                     format_eligible_members,
                                                     DEFAULT_FAILING_STREAK_THRESHOLD)
                        from .recorder import _busy_channel_ids
                        from .routes.channel_tests import _latest_tests_by_channel
                        members = recording_members(rec.group.memberships)
                        latest_by_channel = _latest_tests_by_channel([ch.id for ch in members])
                        # The format lock filters here too, or this pre-checks a member
                        # record start would have skipped - the shape this block promises
                        # to mirror (DESIGN-channel-groups-model.md 5).
                        members = format_eligible_members(rec.group, members,
                                                          latest_by_channel).members
                        busy_ids = _busy_channel_ids(recording_id)
                        streak_threshold = ct_cfg.get(
                            'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
                        best = (pick_best_member(members, latest_by_channel, exclude_ids=busy_ids,
                                                 streak_threshold=streak_threshold)
                                or pick_best_member(members, latest_by_channel,
                                                    streak_threshold=streak_threshold))
                        channel_id = best.id if best is not None else None
                    else:
                        channel_id = rec.channel_id
                except Exception:
                    log.exception('run_pre_check: recording %d - could not resolve a channel',
                                  recording_id)
                    channel_id = None
                    skip_reason = 'Could not resolve a channel to pre-check'
                if channel_id is None:
                    if skip_reason is None:
                        skip_reason = 'No selectable channel to pre-check (direct or via group)'
                    _end_run()

    if skip_reason is not None:
        _pre_check_skip(app, recording_id, rec_name, skip_reason)
        return

    # ── Phase 2: run the test - no outer app context ────────────────────────────
    try:
        test_id = run_channel_test(app, channel_id)
    finally:
        _end_run()

    # ── Phase 3: outcome recording (short-lived context) ────────────────────────
    with app.app_context():
        from . import db
        from .database import TEST_STATUS_COMPLETED, TEST_STATUS_FAILED

        if test_id is None:
            _pre_check_skip(app, recording_id, rec_name,
                            'Could not acquire a connection slot for the pre-check')
            return

        test = db.session.get(ChannelTest, test_id)
        if test is None:
            return
        channel = db.session.get(Channel, channel_id)

        if test.status == TEST_STATUS_COMPLETED:
            from .channel_groups import effective_score
            score = effective_score(channel) if channel is not None else None
            detail = f'{channel.name if channel else "channel"} tested OK (effective score {score})'

            @retry_on_locked()
            def _finish_passed():
                t = db.session.get(ChannelTest, test_id)
                if t is not None:
                    t.pre_check_recording_id = recording_id
                add_recording_event(recording_id, PRE_CHECK_PASSED, detail=detail)
                db.session.commit()
            _finish_passed()
        elif test.status == TEST_STATUS_FAILED:
            from .config import load_config
            from .health_score import channel_failing_reason
            reason = (channel_failing_reason(test, channel, load_config())
                     if channel is not None else test.error_detail)
            detail = f'{channel.name if channel else "channel"}: {reason or test.error_detail}'

            @retry_on_locked()
            def _finish_failed():
                t = db.session.get(ChannelTest, test_id)
                if t is not None:
                    t.pre_check_recording_id = recording_id
                add_recording_event(recording_id, PRE_CHECK_FAILED, detail=detail)
                db.session.commit()
            _finish_failed()
        else:
            # CANCELLED - most likely preempted by the very recording it protects (P2).
            # Not a channel verdict, so it is observable as a skip rather than a failure;
            # still stamp provenance so the test row shows which recording it served.
            @retry_on_locked()
            def _finish_cancelled():
                t = db.session.get(ChannelTest, test_id)
                if t is not None:
                    t.pre_check_recording_id = recording_id
                db.session.commit()
            _finish_cancelled()
            cancel_reason = test.error_detail or 'aborted'
            skip_reason = f'Pre-check was cancelled before it finished ({cancel_reason})'

    if skip_reason is not None:
        _pre_check_skip(app, recording_id, rec_name, skip_reason)


def run_single_channel_test(app, channel_id: int) -> Optional[int]:
    """Run one channel's test on its own, outside any health check ("Test now" on the
    channel detail page).

    Deliberately NOT a one-channel OnDemandTestJob: a job would leave a permanent
    group and job row behind for every click. The result is a normal
    ChannelTest with job_id NULL, so it feeds the lifetime score and shows up in the
    channel's Test History exactly like any other observation.

    Sets the shared run state so the tester is visibly busy for the duration - a one-off
    test occupies the same single-run-globally slot as a health check, and
    run_channel_test() enforces the account connection limit underneath. Returns the
    ChannelTest id, or None when the run never happened (already busy, channel gone, or
    the account was at its connection limit)."""
    with _lock:
        if _state.is_running:
            return None
        _reset_run_state(run_kind='one_off', label='one-off test')
        _state.total_channels = 1

    # Everything below is guarded, not just the test: the run is already registered above,
    # so a raise anywhere past that point - a contended-SQLite OperationalError on the
    # channel fetch is the realistic one - would carry is_running=True and the KIND_TESTER
    # ticket out of this bare thread with no teardown, deferring every later sync and
    # maintenance job for the life of the process (dev/changelog/722).
    try:
        with app.app_context():
            from . import db
            from .database import Channel
            ch = db.session.get(Channel, channel_id)
            name = ch.name if ch is not None else ''
            url = ch.stream_url if ch is not None else ''

        with _lock:
            _state.current_phase = 'testing'
            _state.current_channel_id = channel_id
            _state.current_channel_name = name
            _state.current_channel_url = url

        _append_log('INFO', f'One-off test: {name}')
        return run_channel_test(app, channel_id)
    except Exception:
        # Logged, then re-raised - never swallowed. This runs on a bare daemon thread with
        # no excepthook, so an unlogged raise reaches stderr and nothing else, while the
        # page that started the test waits for a refresh that never comes. The app context
        # is what lets the ERROR record reach the log-to-alert handler in app/__init__.py.
        with app.app_context():
            log.exception('One-off test for channel %d failed', channel_id)
        _append_log('ERROR', 'One-off test failed - see the application log for details')
        raise
    finally:
        with _lock:
            _state.completed_channels = 1
        _end_run()


def run_channel_test(app, channel_id: int, job_id: Optional[int] = None,
                     defer_group_format: bool = False) -> Optional[int]:
    """Run a quality test for a single channel, respecting the channel's account
    connection limit (shared with recordings - see app/connection_limits.py).

    A thin wrapper around _run_channel_test_inner(): resolves the channel's
    account and tries to reserve a connection slot before doing any real work. If
    the account is already at its limit (e.g. a recording is using its one slot),
    the test is skipped rather than run - _run_channel_loop() already treats a
    plain return here as "done with this channel, move to the next." The slot is
    always released via `finally`, however the inner test finishes (including via
    a recording preempting it mid-test through kill_active_test_for_account()).

    Returns the created ChannelTest.id, or None if the test never ran (channel
    not found, or the account was at its connection limit) - run_pre_check needs
    to distinguish "ran" from "skipped at the slot"; _run_channel_loop ignores
    the return value entirely.

    `defer_group_format` leaves this test's groups to the caller's own end-of-run settle
    pass rather than reconciling them here - see _settle_group_formats. A separate
    parameter rather than a read of `job_id`, because "which health check produced this
    test" and "will somebody else settle the groups afterward" are two questions and one
    flag cannot mean both.
    """
    from . import connection_limits as connlim
    with app.app_context():
        from . import db
        from .database import Channel
        ch = db.session.get(Channel, channel_id)
        if ch is None:
            log.error('run_channel_test: channel %d not found', channel_id)
            _append_log('ERROR', f'Channel {channel_id} not found in database')
            return None
        account_id = ch.account_id

        if not connlim.try_acquire(account_id, 'test', channel_id):
            log.info('Channel %d: skipping test - account %d at its connection limit', channel_id, account_id)
            _append_log('WARN', 'Skipped test - account at its connection limit (in use by a recording)')
            return None

    # Register the account the instant the slot is held, not later alongside the
    # ffmpeg proc: kill_active_test_for_account() matches on this field, so any gap
    # between acquire and registration is a window where a preempting recording
    # strips the slot silently and the test launches ffmpeg without one.
    with _lock:
        _state.active_test_account_id = account_id

    try:
        return _run_channel_test_inner(app, channel_id, job_id=job_id,
                                       defer_group_format=defer_group_format)
    finally:
        with app.app_context():
            connlim.release(account_id, 'test', channel_id)
        with _lock:
            _state.active_test_proc = None
            _state.active_test_account_id = None


def _run_channel_test_inner(app, channel_id: int, job_id: Optional[int] = None,
                            defer_group_format: bool = False):
    """Run a quality test for a single channel and persist results to ChannelTest.

    `defer_group_format`: see run_channel_test."""
    with app.app_context():
        from . import db
        from .config import load_config, resolve_ffmpeg_path
        from .database import (
            Channel, ChannelTest, OnDemandTestJob,
            TEST_STATUS_COMPLETED, TEST_STATUS_FAILED, TEST_STATUS_CANCELLED,
        )

        ch = db.session.get(Channel, channel_id)
        if ch is None:
            log.error('run_channel_test: channel %d not found', channel_id)
            _append_log('ERROR', f'Channel {channel_id} not found in database')
            return

        cfg = load_config()
        from .accounts import normalize_url
        stream_url = normalize_url(ch.stream_url, ch.account, cfg)
        with _lock:
            # Overrides the raw value _run_channel_loop set before this test started -
            # this is the URL actually handed to ffmpeg below, so the live status/log
            # must reflect any normalization-mode change too, not just the stored spelling.
            _state.current_channel_url = stream_url
        ct_cfg = cfg.get('channel_testing', {})
        job = db.session.get(OnDemandTestJob, job_id) if job_id else None
        settings = resolve_health_check_settings(ct_cfg, job.profile if job else None)
        duration = settings['test_duration_seconds']
        screenshots_enabled = settings['screenshots_enabled']
        screenshot_dir = ct_cfg.get('screenshot_dir', '/dvr/channel_test_screenshots')
        keep_screenshots = ct_cfg.get('screenshots_keep_count', 5)
        keep_history = ct_cfg.get('test_history_keep', 0)
        connect_retries = settings['connect_retries']
        connect_timeout = settings['connect_timeout_seconds']
        retry_delay = settings['connect_retry_delay_seconds']
        bitrate_fail_720p  = ct_cfg.get('bitrate_fail_720p_kbps', 1000)
        bitrate_fail_1080p = ct_cfg.get('bitrate_fail_1080p_kbps', 2000)
        bitrate_fail_4k    = ct_cfg.get('bitrate_fail_4k_kbps', 3000)
        ffmpeg_path = resolve_ffmpeg_path(cfg['ffmpeg']['path'])
        stall_threshold = 15

        test_started = datetime.utcnow()

        with _lock:
            _state.current_test_started_at = test_started
            _state.current_test_drop_count = 0
            _state.current_test_screenshot_path = None

        from .db_utils import retry_on_locked

        @retry_on_locked()
        def _create_test_row_and_commit():
            test = ChannelTest(
                channel_id=channel_id,
                job_id=job_id,
                test_started_at=test_started,
                status=TEST_STATUS_FAILED,
                drop_count=0,
            )
            db.session.add(test)
            db.session.commit()
            return test.id

        test_id = _create_test_row_and_commit()

        # A bounded test can be 100-200+ MB and a health-check run does this once per
        # channel for hours; '' (the default) uses the system temp dir, same as any
        # other bounded scratch file. channel_testing.capture_scratch_dir lets an
        # operator redirect it if their system temp dir is constrained.
        scratch_dir = ct_cfg.get('capture_scratch_dir') or None
        if scratch_dir:
            os.makedirs(scratch_dir, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.ts', prefix=f'ch_test_{channel_id}_',
                                            dir=scratch_dir)
        os.close(tmp_fd)

        proc = None
        connected = False
        drop_count = 0
        error_detail = None
        quality_fail_msg = None
        bytes_received = 0
        resolution = None
        fps = None
        bitrate_kbps = None
        actual_duration = None
        frame_count = None
        frame_pct = None
        expected_frames = None
        # Stream quality profile (DESIGN-stream-quality-profile.md) - informational only.
        video_codec = None
        pix_fmt = None
        bit_depth = None
        chroma_subsampling = None
        interlaced = None
        coded_resolution = None
        is_vfr = None
        bpp_frame = None
        timeline_gap_count = None
        timeline_gap_seconds = None
        video_track_count = None
        audio_track_count = None
        extra_tracks_json = None
        screenshot_path = None
        status = TEST_STATUS_FAILED
        connect_attempts_used = 0
        probe = {}

        from .proc_utils import build_capture_cmd
        # Paced: a bounded test must occupy its configured wall-clock duration or stall
        # detection and bitrate sampling run over an abbreviated window
        # (dev/docs/BUGS.md 2026-06-28 04:40 pm).
        cmd = build_capture_cmd(cfg, stream_url, tmp_path, duration, pace_realtime=True)

        max_attempts = connect_retries + 1
        with _lock:
            _state.max_connect_attempts = max_attempts

        try:
            launch_mono = None
            stderr_thread = None

            for attempt in range(max_attempts):
                if _is_stop_requested():
                    break
                if _is_preempted():
                    break

                connect_attempts_used = attempt + 1
                with _lock:
                    _state.current_connect_attempt = connect_attempts_used

                if max_attempts > 1:
                    _append_log('INFO', f'Connection attempt {connect_attempts_used}/{max_attempts}')
                else:
                    _append_log('INFO', 'Connecting to stream…')

                # Clear temp file for a fresh attempt
                try:
                    open(tmp_path, 'wb').close()
                except OSError:
                    pass

                preempted_after_register = False
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,  # drained by _drain_stderr thread below
                        text=True,
                    )
                    stderr_buf: collections.deque = collections.deque(maxlen=50)
                    stderr_thread = threading.Thread(
                        target=_drain_stderr,
                        args=(proc.stderr, stderr_buf),
                        daemon=True,
                    )
                    stderr_thread.start()
                    with _lock:
                        _state.active_test_proc = proc
                        _state.active_test_account_id = ch.account_id
                        # Re-read the flag in the SAME locked block that registers the
                        # proc. This is what makes the race airtight: a preempting
                        # recording either sees the registered proc and kills it, or set
                        # the flag before registration and we kill our own proc one
                        # instruction later. Never split this read into its own lock.
                        preempted_after_register = _state.preempted_by_recording
                except Exception as exc:
                    error_detail = f'Failed to launch ffmpeg: {mask_creds_in_text(str(exc))}'
                    log.error('Channel %d: %s', channel_id, error_detail)
                    _append_log('ERROR', error_detail)
                    _append_log('ERROR', f'Stream URL: {mask_creds(stream_url)}')
                    break

                if preempted_after_register:
                    terminate_or_kill(proc)
                    proc = None
                    with _lock:
                        _state.active_test_proc = None
                    break

                launch_mono = time.monotonic()
                got_byte = wait_for_file_data(lambda: tmp_path, connect_timeout,
                                              stop_check=_is_stop_requested, proc=proc)

                if got_byte:
                    _append_log('INFO', 'Stream connected' + (f' on attempt {connect_attempts_used}' if connect_attempts_used > 1 else ''))
                    connected = True
                    break

                # No data - classify failure, kill, and maybe retry
                exit_code_before_kill = proc.poll()
                if exit_code_before_kill is None:
                    # ffmpeg still running: server never sent data (hung connection)
                    terminate_or_kill(proc, hard=True)
                    fail_class = 'Connection timed out - server did not respond (hung connection)'
                else:
                    fail_class = None  # filled below after stderr drain

                stderr_thread.join(timeout=2.0)
                stderr_snippet = _extract_stderr_error(list(stderr_buf))

                if exit_code_before_kill is not None:
                    code_str = f' (exit code {exit_code_before_kill})'
                    if stderr_snippet:
                        fail_class = f'Connection failed - ffmpeg exited with error: {stderr_snippet}{code_str}'
                    else:
                        fail_class = f'Connection failed - ffmpeg exited immediately{code_str}'

                proc = None
                with _lock:
                    _state.active_test_proc = None

                if attempt < max_attempts - 1:
                    _append_log('WARN', f'Attempt {connect_attempts_used}/{max_attempts}: {fail_class} - waiting {retry_delay}s before retry')
                    _interruptible_sleep(retry_delay)
                    # proc is None across the sleep, so a preemption landing here is
                    # exactly the G2 window - re-check before looping into another Popen.
                    if _is_preempted():
                        break
                else:
                    error_detail = f'No data received after {connect_attempts_used} connection attempt{"s" if connect_attempts_used != 1 else ""}: {fail_class}'
                    _append_log('ERROR', error_detail)
                    _append_log('ERROR', f'URL: {mask_creds(stream_url)}')

            if connected and proc:
                # Stall-poll for remaining duration. The floor keeps a slow-connecting
                # test polling for a useful window, but never past the length the test
                # asked for - a 5s profile that sat here for a 10s minimum would spend
                # more time watching a finished capture than making one.
                elapsed = time.monotonic() - launch_mono if launch_mono else 0
                remaining = max(min(10.0, duration), duration - elapsed)
                drop_count = _poll_for_stalls(
                    tmp_path,
                    duration=remaining,
                    stall_threshold=stall_threshold,
                    proc=proc,
                    stop_check=_is_stop_requested,
                )

            if os.path.exists(tmp_path):
                try:
                    bytes_received = os.path.getsize(tmp_path)
                except OSError:
                    bytes_received = 0

            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    terminate_or_kill(proc, hard=True)
                proc = None
                with _lock:
                    _state.active_test_proc = None

            if stderr_thread is not None:
                stderr_thread.join(timeout=2.0)

            if connected:
                _append_log('INFO', f'Recording complete - {bytes_received:,} bytes received')

            if connected:
                try:
                    probe = _parse_ffprobe(tmp_path)
                    resolution = probe.get('resolution')
                    fps = probe.get('fps')
                    actual_duration = probe.get('duration')
                    frame_count = probe.get('frame_count')

                    # Stream quality profile - reads more of the same ffprobe JSON, no
                    # extra decode. Informational only (never feeds health_score).
                    video_codec = probe.get('video_codec')
                    pix_fmt = probe.get('pix_fmt')
                    bit_depth = probe.get('bit_depth')
                    chroma_subsampling = probe.get('chroma_subsampling')
                    interlaced = probe.get('interlaced')
                    coded_resolution = probe.get('coded_resolution')
                    is_vfr = probe.get('is_vfr')

                    # Multi-track detection (dev/changelog/564) - video_codec/audio_codec
                    # above already describe track 0; this captures everything beyond it.
                    probe_video_tracks = probe.get('video_tracks') or []
                    probe_audio_tracks = probe.get('audio_tracks') or []
                    video_track_count = len(probe_video_tracks) or None
                    audio_track_count = len(probe_audio_tracks) or None
                    extra_tracks = (
                        [{'type': 'video', **t} for t in probe_video_tracks[1:]]
                        + [{'type': 'audio', **t} for t in probe_audio_tracks[1:]]
                    )
                    if extra_tracks:
                        extra_tracks_json = json.dumps(extra_tracks)
                        _append_log('INFO', f'{len(probe_video_tracks)} video, '
                                    f'{len(probe_audio_tracks)} audio track(s) detected')

                    if actual_duration is not None:
                        bitrate_kbps = (bytes_received * 8) / actual_duration / 1000 if actual_duration > 0 else None
                    else:
                        bitrate_kbps = (bytes_received * 8) / duration / 1000

                    # Quality-profile stats (informational). bits/pixel/frame is a pure
                    # efficiency number; the timeline scan is a second ffprobe over the
                    # same clip (reuse of the seek-damage scanner), bounded by clip length.
                    from .probe import (bits_per_pixel_frame, expected_frame_count,
                                        scan_video_timeline)
                    if resolution and fps and bitrate_kbps:
                        try:
                            w_px, h_px = (int(x) for x in resolution.split('x'))
                            bpp_frame = bits_per_pixel_frame(bitrate_kbps * 1000, w_px, h_px, fps)
                        except (ValueError, AttributeError):
                            bpp_frame = None
                    timeline = scan_video_timeline(tmp_path)
                    if timeline:
                        timeline_gap_count = timeline.get('gap_count')
                        timeline_gap_seconds = timeline.get('gap_seconds')

                    # Scanned before this, not after: the decode span it measures is the
                    # only honest denominator here, and the container duration it replaces
                    # cost every short test a few points of health score for frames that
                    # were never missing (dev/changelog/896).
                    if frame_count:
                        expected_frames = expected_frame_count(
                            fps,
                            dts_span_seconds=(timeline or {}).get('dts_span_seconds'),
                            fallback_duration=actual_duration)
                        if expected_frames and expected_frames > 0:
                            frame_pct = round(frame_count / expected_frames * 100, 1)

                    if resolution:
                        fps_str = f'{fps:.1f}fps' if fps else 'unknown fps'
                        dur_str = f'{actual_duration:.0f}s' if actual_duration else '?s'
                        frame_str = f'  |  frames: {frame_count:,}/{int(expected_frames):,} ({frame_pct:.1f}%)' if frame_pct is not None else ''
                        _append_log('INFO', f'Video: {resolution} @ {fps_str}  |  bitrate: {bitrate_kbps:.0f} kbps  |  duration: {dur_str}{frame_str}')
                    else:
                        _append_log('WARN', empty_probe_warning())
                    if probe.get('audio_codec'):
                        ach = probe.get('audio_channels') or 0
                        asr = probe.get('audio_sample_rate') or 0
                        abr = probe.get('audio_bitrate_kbps')
                        lang = probe.get('audio_language') or ''
                        a_parts = [probe['audio_codec'].upper()]
                        if ach:
                            a_parts.append(f'{ach}ch')
                        if asr:
                            a_parts.append(f'{asr // 1000}kHz')
                        if abr:
                            a_parts.append(f'{abr:.0f}kbps')
                        if lang:
                            a_parts.append(lang)
                        _append_log('INFO', 'Audio: ' + ' '.join(a_parts))
                except Exception as exc:
                    _append_log('WARN', f'ffprobe failed: {exc}')

            if connected and bitrate_kbps is not None and resolution:
                try:
                    h = int(resolution.split('x')[1])
                    if h <= 720:
                        br_threshold = bitrate_fail_720p
                        tier_label = '≤720p'
                    elif h <= 1080:
                        br_threshold = bitrate_fail_1080p
                        tier_label = '≤1080p'
                    else:
                        br_threshold = bitrate_fail_4k
                        tier_label = '>1080p'
                except (ValueError, AttributeError, IndexError):
                    br_threshold = bitrate_fail_720p
                    tier_label = 'unknown'
                if bitrate_kbps <= br_threshold:
                    fail_msg = f'Low bitrate: {bitrate_kbps:.0f} kbps for {resolution} ({tier_label} threshold: {br_threshold} kbps)'
                    error_detail = (error_detail + '; ' + fail_msg) if error_detail else fail_msg
                    quality_fail_msg = fail_msg
                    _append_log('ERROR', fail_msg)

            if connected and actual_duration is not None:
                verdict, verdict_msg = short_capture_verdict(actual_duration, duration)
                if verdict == 'fail':
                    connected = False
                    status = TEST_STATUS_FAILED
                    error_detail = verdict_msg
                    _append_log('ERROR', f'{verdict_msg} - marking FAILED')
                elif verdict == 'warn':
                    error_detail = verdict_msg
                    _append_log('WARN', verdict_msg)

            if screenshots_enabled and connected and bytes_received > 0:
                shot_name = f'ch_{channel_id}_{test_started.strftime("%Y%m%d_%H%M%S")}.jpg'
                shot_path = os.path.join(screenshot_dir, shot_name)
                shot_ok, color_warn, recaptured = _capture_screenshot_with_recapture(
                    tmp_path, shot_path, ffmpeg_path, probe, actual_duration)
                if shot_ok:
                    screenshot_path = shot_path
                    with _lock:
                        _state.current_test_screenshot_path = shot_path
                    if color_warn:
                        note = ' - recaptured from later in the clip, still blank' if recaptured else ''
                        full_warn = color_warn + note
                        _append_log('WARN', full_warn)
                        error_detail = (error_detail + '; ' + full_warn) if error_detail else full_warn
                    elif recaptured:
                        _append_log('WARN', 'First screenshot was blank/uniform - recaptured from later in the clip and found real content')
                    else:
                        _append_log('INFO', 'Screenshot captured')
                else:
                    _append_log('WARN', 'Screenshot capture failed (stream may be audio-only or clip too short)')

            with _lock:
                was_preempted, _state.preempted_by_recording = _state.preempted_by_recording, False
                # Deregister the account in the SAME locked block that consumes the flag.
                # This test can no longer act on a preemption, so from here on
                # kill_active_test_for_account() must not match it: a preempt landing in
                # the finalize tail below (health scoring, group recheck, screenshot
                # cleanup - seconds of DB work) would otherwise set a flag with no
                # consumer left, and the NEXT channel would abort at its connect loop and
                # be recorded CANCELLED for a recording that started before it existed.
                # Safe because the capture ffmpeg is already dead and deregistered above,
                # and connection_limits.preempt_tests_for_slot() strips the slot from
                # _holders whatever this helper answers, so the recording still gets it.
                _state.active_test_account_id = None
            # An aborted test says nothing about the channel, so it must never reach
            # 'FAILED' - score_test_quality applies the fail floor to every FAILED test
            # and would tank the health score of a perfectly good channel. Both aborts
            # are external: a recording reclaiming the slot, or the user stopping the run.
            cancelled = was_preempted or _is_stop_requested()
            if was_preempted:
                connected = False
                error_detail = 'Interrupted by recording start (connection slot needed by a recording)'
                _append_log('WARN', 'Test aborted - connection slot reclaimed by a starting recording')
            elif cancelled:
                connected = False
                error_detail = 'Cancelled before the test finished (run stopped by user)'
                _append_log('WARN', 'Test cancelled - run stopped by user')

            if cancelled:
                status = TEST_STATUS_CANCELLED
            else:
                status = TEST_STATUS_COMPLETED if (connected and not quality_fail_msg) else TEST_STATUS_FAILED
            if status == TEST_STATUS_CANCELLED:
                _append_log('WARN', f'CANCELLED - {error_detail}')
            elif status == TEST_STATUS_COMPLETED:
                parts = []
                if resolution:
                    parts.append(resolution)
                if fps:
                    parts.append(f'{fps:.0f}fps')
                if bitrate_kbps:
                    parts.append(f'{bitrate_kbps:.0f}kbps')
                if actual_duration:
                    parts.append(f'{actual_duration:.0f}s')
                if frame_pct is not None:
                    parts.append(f'{frame_pct:.1f}% frames')
                parts.append(f'{drop_count} drop{"s" if drop_count != 1 else ""}')
                _append_log('SUCCESS', 'PASSED - ' + ' · '.join(parts))
            else:
                _append_log('ERROR', f'FAILED - {error_detail or "no data received"}')

            log.info(
                'Channel %d (%s): %s - res=%s fps=%s bitrate=%.0f kbps drops=%d dur=%s frame_pct=%s attempts=%d',
                channel_id, ch.name, status,
                resolution or 'n/a',
                f'{fps:.1f}' if fps else 'n/a',
                bitrate_kbps or 0,
                drop_count,
                f'{actual_duration:.0f}s' if actual_duration else 'n/a',
                f'{frame_pct:.1f}%' if frame_pct is not None else 'n/a',
                connect_attempts_used,
            )

        except Exception as exc:
            log.exception('Unexpected error testing channel %d: %s', channel_id, exc)
            terminate_or_kill(proc, hard=True)
            # log.exception is masked on its way to dvr.log by CredentialMaskingFilter,
            # but _append_log and error_detail below bypass logging entirely (the ring
            # buffer and ChannelTest.error_detail are not log handlers), so exc must be
            # masked explicitly here too.
            error_detail = mask_creds_in_text(str(exc))
            # Consume the flag here too: an exception raised before the normal consumer
            # block would otherwise leave it set for the NEXT channel to pick up, marking
            # an unrelated test CANCELLED. An abort that surfaced as an exception is still
            # an abort, so it must not take the fail floor either.
            with _lock:
                was_preempted, _state.preempted_by_recording = _state.preempted_by_recording, False
                _state.active_test_account_id = None  # same reason as the normal path above
            status = TEST_STATUS_CANCELLED if (was_preempted or _is_stop_requested()) else TEST_STATUS_FAILED
            _append_log('ERROR', f'Unexpected error: {error_detail}')

        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass  # best-effort temp-file cleanup

        _finalize_test(app, test_id, connected, drop_count, bitrate_kbps,
                       resolution, fps, actual_duration, connect_attempts_used,
                       screenshot_path, status, error_detail,
                       probe.get('audio_codec'), probe.get('audio_channels'),
                       probe.get('audio_sample_rate'), probe.get('audio_bitrate_kbps'),
                       probe.get('audio_language'),
                       test_started, frame_count, frame_pct,
                       quality_profile={
                           'video_codec': video_codec,
                           'pix_fmt': pix_fmt,
                           'bit_depth': bit_depth,
                           'chroma_subsampling': chroma_subsampling,
                           'interlaced': interlaced,
                           'coded_resolution': coded_resolution,
                           'is_vfr': is_vfr,
                           'bits_per_pixel_frame': bpp_frame,
                           'timeline_gap_count': timeline_gap_count,
                           'timeline_gap_seconds': timeline_gap_seconds,
                           'video_track_count': video_track_count,
                           'audio_track_count': audio_track_count,
                           'extra_tracks': extra_tracks_json,
                       })

        from .health_score import apply_test_health_observation, assess_scheduled_recording_impact
        apply_test_health_observation(app, test_id)
        assess_scheduled_recording_impact(app, test_id)

        # A test inside a health check run leaves this to that run's own settle pass, which
        # runs once at the end over a complete test map (_settle_group_formats). A one-off
        # test or a pre-check IS the whole run, so it reconciles here and now.
        if not defer_group_format:
            _recheck_group_format(app, channel_id)

        if screenshots_enabled:
            _cleanup_old_screenshots(app, channel_id, job_id, screenshot_dir, keep_screenshots)

        if keep_history > 0:
            _cleanup_old_tests(app, channel_id, job_id, keep_history)

        return test_id


def _recheck_group_format(app, channel_id: int):
    """Deferred format re-check: a member admitted with unknown format can only be proven
    a mismatch once a health test finally measures it. After each test lands, if this
    channel is in a group, re-run the reconcile engine for the whole group. Runs
    regardless of whether THIS channel is the outlier - its now-known format can change
    the derived reference, or let another member start conforming - and
    evaluate_and_reconcile_group owns the mismatch/resolve ChannelGroupEvent log entries
    and GROUP_FORMAT_MISMATCH alerts (Part E).

    Every group the channel belongs to is evaluated, which is why Part E's transition
    state has to be per-membership: these calls run back to back, so a channel-scoped
    state let each group overwrite the previous one's verdict (dev/changelog/789).
    Best-effort - a failure here must never break the test run."""
    with app.app_context():
        from . import db
        from .config import load_config
        from .database import Channel
        from .channel_groups import evaluate_and_reconcile_group, DEFAULT_FAILING_STREAK_THRESHOLD

        try:
            ch = db.session.get(Channel, channel_id)
            if ch is None:
                return
            # Hoisted once for this channel's groups (CLAUDE.md no-hidden-I/O-in-loops).
            streak_threshold = load_config().get('channel_testing', {}).get(
                'failing_streak_threshold', DEFAULT_FAILING_STREAK_THRESHOLD)
            # A channel may be in several groups now - re-evaluate each one.
            for m in ch.group_memberships:
                evaluate_and_reconcile_group(m.group, streak_threshold)
        except Exception:
            log.exception('group format re-check failed for channel %d', channel_id)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _finalize_test(app, test_id, connected, drop_count, bitrate_kbps,
                   resolution, fps, duration_seconds, connect_attempts,
                   screenshot_path, status, error_detail,
                   audio_codec, audio_channels, audio_sample_rate,
                   audio_bitrate_kbps, audio_language,
                   test_started, frame_count=None, frame_pct=None,
                   quality_profile=None):
    # quality_profile: dict of the DESIGN-stream-quality-profile.md §2 fields plus the
    # multi-track columns (video_track_count/audio_track_count/extra_tracks), or None
    # (never measured, e.g. a failed connect). Only its keys that match ChannelTest
    # columns are applied.
    quality_profile = quality_profile or {}
    with app.app_context():
        from . import db
        from .database import ChannelTest
        from .db_utils import retry_on_locked

        @retry_on_locked()
        def _persist_results_and_commit():
            test = db.session.get(ChannelTest, test_id)
            if test is None:
                return
            test.test_ended_at = datetime.utcnow()
            test.connected = connected
            test.drop_count = drop_count or 0
            test.bitrate_kbps = bitrate_kbps
            test.resolution = resolution
            test.fps = fps
            test.duration_seconds = duration_seconds
            test.connect_attempts = connect_attempts or 1
            test.screenshot_path = screenshot_path
            test.status = status
            test.error_detail = error_detail
            test.audio_codec = audio_codec
            test.audio_channels = audio_channels
            test.audio_sample_rate = audio_sample_rate
            test.audio_bitrate_kbps = audio_bitrate_kbps
            test.audio_language = audio_language
            test.frame_count = frame_count
            test.frame_pct = frame_pct
            for col, value in quality_profile.items():
                setattr(test, col, value)
            db.session.commit()

        _persist_results_and_commit()


def _is_stop_requested() -> bool:
    with _lock:
        return _state.stop_requested


def _is_preempted() -> bool:
    """Peek at the preemption flag without consuming it. The single consumer is
    the end-of-test block in _run_channel_test_inner, which flips it back to False
    while turning it into the channel's failure result - this must not clear it or
    that result is lost and the channel reports a bare connection failure."""
    with _lock:
        return _state.preempted_by_recording


def _drain_stderr(pipe, buf: collections.deque):
    """Read stderr lines into buf until EOF. Runs in a background thread."""
    try:
        for line in pipe:
            buf.append(line.rstrip('\n'))
    except (OSError, ValueError):
        pass  # pipe closed when the ffmpeg process exits - nothing left to drain


def _extract_stderr_error(lines: list) -> str:
    """Return the most relevant error line from ffmpeg stderr, or empty string.

    The keyword list below includes 'http', so the line most likely to be
    selected is often the one embedding the full stream URL - ffmpeg routinely
    prints the input URL in its error lines. Mask before returning: this value
    flows into ChannelTest.error_detail, the live run log, and recording
    events, and stream-URL path segments are credentials for Xtream accounts."""
    ERROR_KEYWORDS = ('error', 'http', 'connection', 'refused', 'timeout',
                      'forbidden', '403', '404', '401', '500', 'failed', 'invalid')
    best = None
    for line in reversed(lines):
        if any(k in line.lower() for k in ERROR_KEYWORDS):
            best = line.strip()
            break
    if best is None:
        for line in reversed(lines):
            if line.strip():
                best = line.strip()
                break
    best = mask_creds_in_text(best) if best else best
    return (best[:200] + '…') if best and len(best) > 200 else (best or '')


def _poll_for_stalls(filepath: str, duration: float, stall_threshold: float,
                     proc, stop_check) -> int:
    """Poll file size growth; return count of stall events detected.

    A stall is when the file hasn't grown for >= stall_threshold seconds.
    We count it and reset (no restart - this is a bounded test).

    The tick scales down with the window so a short test isn't held here for a full 5s
    after ffmpeg has already exited; stall semantics are unaffected, since GrowthMonitor
    measures elapsed time rather than counting ticks. Unchanged at 20s and above.
    """
    POLL_INTERVAL = min(5.0, max(0.5, duration / 4))
    drop_count = 0
    monitor = GrowthMonitor()
    deadline = time.monotonic() + duration + 15

    while time.monotonic() < deadline:
        if stop_check():
            terminate_or_kill(proc)
            break

        if proc and proc.poll() is not None:
            break

        try:
            current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
        except OSError:
            current_size = 0

        with _lock:
            _state.current_live_bytes = current_size

        if monitor.update(current_size) >= stall_threshold:
            drop_count += 1
            with _lock:
                _state.current_test_drop_count = drop_count
            _append_log('WARN', f'Stall #{drop_count} detected - stream paused at {monitor.last_size:,} bytes')
            log.debug('Channel test stall #%d detected at %d bytes', drop_count, monitor.last_size)
            monitor.reset()

        time.sleep(POLL_INTERVAL)

    return drop_count


def _parse_ffprobe(filepath: str) -> dict:
    """Thin wrapper - delegates to the shared probe utility."""
    from .probe import parse_ffprobe
    return parse_ffprobe(filepath)



def _capture_screenshot(filepath: str, output_path: str, ffmpeg_path: str,
                        probe: Optional[dict] = None,
                        seek_args: Optional[list] = None) -> bool:
    """Extract a single JPEG frame from near the 5s mark (or seek_args, if given).

    Thin delegate - the real implementation lives in screenshot.py (shared with
    the recording-detail live-thumbnail route, which needs a different seek point).
    """
    from .screenshot import capture_screenshot
    return capture_screenshot(filepath, output_path, ffmpeg_path, probe=probe, seek_args=seek_args)


def _capture_screenshot_with_recapture(filepath: str, output_path: str, ffmpeg_path: str,
                                       probe: Optional[dict], actual_duration: Optional[float]
                                       ) -> tuple:
    """Capture a screenshot, and if it comes back blank/uniform, try once more from later
    in the same already-recorded clip before giving up.

    The clip at `filepath` is the test's full local recording, not a live stream, so a
    recapture is just a second ffmpeg seek+grab on disk - no new connection, no
    connection-limit interaction. Returns (success, warning, recaptured):
    - success: whether a screenshot file exists at output_path at all.
    - warning: the blank/uniform message if the frame ultimately kept is still uniform,
      else None.
    - recaptured: whether a second capture attempt was made.
    """
    from .screenshot import seek_args_for_clip
    first_seek_args = seek_args_for_clip(actual_duration)
    if not _capture_screenshot(filepath, output_path, ffmpeg_path, probe=probe,
                               seek_args=first_seek_args):
        return False, None, False
    warn = _check_screenshot_uniform(output_path, ffmpeg_path)
    if not warn:
        return True, None, False

    # Only worth retrying if there's real distance from the first seek point to move to -
    # a short clip has nowhere else to grab from.
    first_seek = float(first_seek_args[1])
    if not actual_duration or actual_duration < first_seek + 5:
        return True, warn, False

    retry_offset = max(first_seek + 1, actual_duration - 3)
    if not _capture_screenshot(filepath, output_path, ffmpeg_path, probe=probe,
                               seek_args=['-ss', str(retry_offset)]):
        return True, warn, False
    warn2 = _check_screenshot_uniform(output_path, ffmpeg_path)
    return True, warn2, True


def _check_screenshot_uniform(filepath: str, ffmpeg_path: str) -> Optional[str]:
    """Return a warning string if the screenshot is a near-uniform color, else None.

    Scales to 64×64 gray (4096 pixels) and computes standard deviation. A std_dev < 15
    indicates a solid or near-solid frame regardless of its average brightness,
    catching black, white, mid-grey, and solid-color test cards alike.

    An 8×8 grid was used previously, but at that resolution each output pixel
    averages such a large source block (~160×90px on a 1280x720 frame) that a
    frame dominated by one color but with real content - e.g. a sports field with
    players, lines, and a scoreboard overlay - gets smoothed into a false "solid
    color" reading. 64×64 preserves enough local detail to tell the two apart while
    still flagging genuinely uniform frames (verified against historical screenshots:
    true blank/solid frames stay under std_dev 11 at this resolution, real
    field-dominant sports frames clear 16.9).
    """
    try:
        cmd = [ffmpeg_path, '-i', filepath,
               '-vf', 'scale=64:64,format=gray',
               '-frames:v', '1', '-f', 'rawvideo', 'pipe:1']
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        if result.returncode == 0 and len(result.stdout) >= 4096:
            pixels = list(result.stdout[:4096])
            avg = sum(pixels) / len(pixels)
            std = (sum((p - avg) ** 2 for p in pixels) / len(pixels)) ** 0.5
            if std < 15:
                if avg < 20:
                    desc = 'nearly all black'
                elif avg > 235:
                    desc = 'nearly all white'
                else:
                    desc = f'appears solid color (avg luma {avg:.0f}/255)'
                return f'Screenshot {desc} - stream may not be showing content'
    except Exception as exc:
        log.warning('Screenshot uniformity check failed for %s: %s', filepath, exc)
    return None


def _job_filter(job_id: Optional[int]):
    """ChannelTest.job_id filter clause - NULL for guide tests, exact match for on-demand jobs."""
    from .database import ChannelTest
    return ChannelTest.job_id.is_(None) if job_id is None else ChannelTest.job_id == job_id


def delete_tests_collecting_screenshots(query):
    """Bulk-delete the ChannelTest rows matched by `query`, returning the screenshot_path
    values the deleted rows carried. `query` must already carry every filter needed - this
    is a plain DB read + write, safe to call from inside a retry_on_locked() closure.

    The caller must unlink the returned paths with app.recorder.delete_files() only AFTER
    that closure's commit has durably succeeded - never from inside it. Unlinking is a
    non-idempotent side effect (CLAUDE.md, tests/test_static_invariants.py::
    RetryOnLockedSideEffectTests), so it cannot live in this function alongside the delete.
    """
    from .database import ChannelTest
    paths = [p for (p,) in query.with_entities(ChannelTest.screenshot_path).all() if p]
    query.delete(synchronize_session=False)
    return paths


def _cleanup_old_screenshots(app, channel_id: int, job_id: Optional[int], screenshot_dir: str, keep_count: int):
    """Delete screenshot files beyond keep_count for this channel *within this job/test
    scope* (job_id=None means the recurring guide test), keeping the newest keep_count.

    Scoped per (channel_id, job_id) rather than per channel_id alone, so a channel
    tested by multiple on-demand jobs and/or the recurring guide test doesn't have one
    job's screenshots evicted by another job's runs. Marks the pruned ChannelTest rows
    so the UI can show a "pruned" placeholder instead of a broken image.
    """
    with app.app_context():
        from . import db
        from .database import ChannelTest
        from .db_utils import retry_on_locked

        tests = (
            ChannelTest.query
            .filter(ChannelTest.channel_id == channel_id, _job_filter(job_id))
            .filter(ChannelTest.screenshot_path.isnot(None))
            .order_by(ChannelTest.test_started_at.desc())
            .all()
        )
        to_prune = tests[keep_count:]
        if not to_prune:
            return

        for t in to_prune:
            try:
                os.unlink(t.screenshot_path)
            except OSError as exc:
                log.debug('Screenshot cleanup: failed to unlink %s: %s', t.screenshot_path, exc)

        prune_ids = [t.id for t in to_prune]

        @retry_on_locked()
        def _mark_pruned_and_commit():
            ChannelTest.query.filter(ChannelTest.id.in_(prune_ids)).update(
                {'screenshot_pruned': True}, synchronize_session=False
            )
            db.session.commit()

        _mark_pruned_and_commit()


def _cleanup_old_tests(app, channel_id: int, job_id: Optional[int], keep_count: int):
    """Delete oldest ChannelTest rows beyond keep_count for this channel *within this
    job/test scope* - same (channel_id, job_id) scoping as _cleanup_old_screenshots,
    so one job's history isn't deleted by another job's tests of the same channel."""
    if keep_count <= 0:
        return
    with app.app_context():
        from . import db
        from .database import ChannelTest
        from .db_utils import retry_on_locked
        from .recorder import delete_files

        @retry_on_locked()
        def _delete_old_tests_and_commit():
            tests = (
                ChannelTest.query
                .filter(ChannelTest.channel_id == channel_id, _job_filter(job_id))
                .order_by(ChannelTest.test_started_at.desc())
                .with_entities(ChannelTest.id)
                .all()
            )
            prune_ids = [t.id for t in tests[keep_count:]]
            if not prune_ids:
                return []
            paths = delete_tests_collecting_screenshots(
                ChannelTest.query.filter(ChannelTest.id.in_(prune_ids)))
            db.session.commit()
            return paths

        delete_files(_delete_old_tests_and_commit())


def _interruptible_sleep(seconds: float):
    """Sleep in 1-second chunks, checking stop_requested each iteration."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with _lock:
            if _state.stop_requested:
                return
        time.sleep(min(1.0, deadline - time.monotonic()))
