"""Live channel preview: watch or listen to a channel's stream in the browser.

A verification tool, not a viewer - the question it answers is "is this the right channel,
and does it play right now?" before a scheduled recording relies on it. One ffmpeg
stream-copies the channel into a rolling HLS window on local disk (proc_utils.
build_preview_cmd), the routes in app/routes/preview.py serve the playlist and segments,
and the browser plays them (hls.js, or Safari natively). The stream URL and its
credentials never reach the browser: it only ever sees this app's own URLs.

Rules this module enforces, each of which a test in tests/test_preview.py guards:

* A preview holds a provider connection slot ('preview' kind, app/connection_limits.py)
  and yields it to a recording the moment one needs it - before a channel test does,
  because a look is worth less than a measurement (recorder._try_acquire_slot_with_
  preemption). It never exceeds the account's limit: at the limit it is refused, naming
  what holds the slots.
* One preview at a time, app-wide. Starting a second stops the first (REASON_REPLACED).
  A single-user app does not need two, and one live session is state anyone can read.
* Every way a preview can end releases the slot, stops ffmpeg and removes the segment
  directory, and every one has a name the modal shows: the user's Stop, the tab closing
  (a beacon, then the idle reaper as the backstop), the idle timeout, the hard cap, ffmpeg
  exiting on its own, connect timeout, preemption, replacement, app shutdown.
* Nothing here feeds the health score or writes a database row. A preview is not an
  observation.

Session state is process-local (like recorder._active): a restart kills the ffmpeg this
process owned, so there is nothing to recover. Full reasoning: dev/changelog/1018.
"""
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import connection_limits as connlim
from .proc_utils import (PREVIEW_PLAYLIST, PREVIEW_SEGMENT_RE, build_preview_cmd,
                         read_stderr_tail, terminate_or_kill)
from .url_utils import mask_creds_in_text

log = logging.getLogger(__name__)

STATE_STARTING = 'STARTING'   # ffmpeg launched, no playable playlist yet
STATE_READY = 'READY'         # the playlist lists at least one segment
STATE_STOPPED = 'STOPPED'     # ended; `reason` says why
STATES = (STATE_STARTING, STATE_READY, STATE_STOPPED)

# Why a session stopped. Every terminal path names one of these, and REASON_TEXT is what
# the modal shows for it - a preview that ends says why, or it is a number nobody can
# explain (CLAUDE.md principle 1).
REASON_USER = 'user'
REASON_IDLE = 'idle'
REASON_MAX_DURATION = 'max_duration'
REASON_CONNECT_TIMEOUT = 'connect_timeout'
REASON_PREEMPTED = 'preempted'
REASON_FFMPEG_EXITED = 'ffmpeg_exited'
REASON_REPLACED = 'replaced'
REASON_SHUTDOWN = 'shutdown'
REASON_LAUNCH_FAILED = 'launch_failed'

REASON_TEXT = {
    REASON_USER: 'Stopped.',
    REASON_IDLE: 'Stopped - nothing has played this preview for a while.',
    REASON_MAX_DURATION: 'Stopped - a preview runs for a limited time. Start it again to keep watching.',
    REASON_CONNECT_TIMEOUT: 'Could not connect - the stream sent nothing playable in time.',
    REASON_PREEMPTED: 'Stopped - a recording needed this connection.',
    REASON_FFMPEG_EXITED: 'The stream ended or dropped.',
    REASON_REPLACED: 'Stopped - another preview was started.',
    REASON_SHUTDOWN: 'Stopped - the app is restarting.',
    REASON_LAUNCH_FAILED: 'Could not start ffmpeg.',
}

# Audio a browser decodes as delivered. Anything else known (ac3, eac3, mp2, dts...) is
# re-encoded to AAC by build_preview_cmd; an UNKNOWN codec (never tested) is copied, because
# 99% of this install's tested channels are AAC and a needless re-encode costs more than a
# rare failed first try that the modal names.
BROWSER_AUDIO_CODECS = frozenset({'aac', 'mp3'})


def audio_needs_transcode(codec) -> bool:
    if not codec:
        return False
    return str(codec).strip().lower() not in BROWSER_AUDIO_CODECS


class PreviewRefused(Exception):
    """A preview could not start; `message` is user-facing prose, `status` the HTTP code."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class PreviewSession:
    id: str
    channel_id: int
    channel_name: str
    account_id: int
    dir: str
    stderr_path: str
    transcode_audio: bool
    idle_timeout: float
    max_seconds: float
    connect_timeout: float
    proc: Optional[subprocess.Popen] = None
    state: str = STATE_STARTING
    reason: Optional[str] = None
    detail: str = ''
    started_mono: float = 0.0
    ready_mono: Optional[float] = None
    last_seen_mono: Optional[float] = None
    # Set by preempt_for_account() while proc is still None - the acquire-to-Popen window.
    # start_preview() re-reads it after Popen and kills its own ffmpeg rather than run it
    # on a slot a recording already took (the tester's G1 fix, DESIGN-concurrency.md 5.1).
    preempted: bool = False
    playlist_fetches: int = 0
    # _finish() claims a session under _lock and tears it down under _teardown_lock, so a
    # second finisher blocks until the first is done rather than returning early.
    _finishing: bool = False
    _teardown_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def live(self) -> bool:
        return self.state != STATE_STOPPED

    @property
    def playlist_path(self) -> str:
        return os.path.join(self.dir, PREVIEW_PLAYLIST)

    def to_dict(self) -> dict:
        now = time.monotonic()
        return {
            'session_id': self.id,
            'channel_id': self.channel_id,
            'channel_name': self.channel_name,
            'state': self.state,
            'reason': self.reason,
            'reason_text': REASON_TEXT.get(self.reason, '') if self.reason else '',
            'detail': self.detail,
            'transcode_audio': self.transcode_audio,
            'elapsed_seconds': round(now - self.started_mono, 1) if self.started_mono else 0,
            'max_seconds': self.max_seconds,
        }


_lock = threading.Lock()
# session id -> PreviewSession. At most one is live; ended ones are kept (bounded) so the
# browser can still ask WHY the session it was playing stopped after the playlist is gone.
_sessions: dict = {}
_reaper: Optional[threading.Thread] = None
_KEEP_ENDED = 5
_REAP_INTERVAL = 0.5


def _live_locked() -> Optional[PreviewSession]:
    for s in _sessions.values():
        if s.live:
            return s
    return None


def live_session() -> Optional[PreviewSession]:
    with _lock:
        return _live_locked()


def get_session(session_id: str) -> Optional[PreviewSession]:
    with _lock:
        return _sessions.get(session_id)


def _prune_ended_locked():
    ended = [s for s in _sessions.values() if not s.live]
    for s in ended[:-_KEEP_ENDED] if len(ended) > _KEEP_ENDED else []:
        _sessions.pop(s.id, None)


def start_preview(channel_id: int) -> PreviewSession:
    """Start previewing `channel_id`, stopping any preview already running. Raises
    PreviewRefused with a user-facing message (404 unknown channel, 409 account at its
    connection limit, 500 the directory or ffmpeg could not be created). Requires an app
    context; the returned session is already registered and reaped."""
    from . import db
    from .config import load_config
    from .database import Channel, ChannelTest
    from .recorder import resolve_capture_pacing

    cfg = load_config()
    pcfg = cfg.get('preview', {}) or {}
    channel = db.session.get(Channel, channel_id)
    if channel is None:
        raise PreviewRefused('Channel not found', 404)
    account = channel.account
    latest = (ChannelTest.query.filter_by(channel_id=channel_id)
              .order_by(ChannelTest.id.desc()).first())
    transcode_audio = audio_needs_transcode(latest.audio_codec if latest else None)
    # The channel's own pacing answer, then the Settings default - the same resolution a
    # recording's unbounded segment gets, so a channel that needs -re to play gets it here too.
    pace, _source = resolve_capture_pacing(cfg, channel.pace_realtime, 0, False)

    stop_all(REASON_REPLACED)

    session = PreviewSession(
        id=secrets.token_urlsafe(12),
        channel_id=channel.id,
        channel_name=channel.name,
        account_id=channel.account_id,
        dir='',
        stderr_path='',
        transcode_audio=transcode_audio,
        idle_timeout=float(pcfg.get('idle_timeout_seconds', 15)),
        max_seconds=float(pcfg.get('max_seconds', 600)),
        connect_timeout=float(pcfg.get('connect_timeout_seconds', 20)),
        started_mono=time.monotonic(),
    )
    # Registered BEFORE the slot is taken, so a recording preempting in the window between
    # acquire and Popen finds something to flag rather than nothing (see `preempted`).
    with _lock:
        _sessions[session.id] = session
        _prune_ended_locked()

    if not connlim.try_acquire(channel.account_id, 'preview', session.id):
        who = connlim.describe_holders(channel.account_id)
        _finish(session, REASON_LAUNCH_FAILED, detail='account at its connection limit')
        raise PreviewRefused(
            f'{account.name} is at its connection limit right now'
            + (f' ({who} is using it)' if who else '') + '. Stop or wait for it to finish.', 409)

    try:
        session.dir = tempfile.mkdtemp(prefix='channelbin-preview-', dir=pcfg.get('dir') or None)
    except OSError as exc:
        _finish(session, REASON_LAUNCH_FAILED, detail=str(exc))
        raise PreviewRefused(f'Could not create the preview directory: {exc}', 500)
    session.stderr_path = os.path.join(session.dir, 'ffmpeg.stderr')

    cmd = build_preview_cmd(cfg, channel.stream_url, session.dir,
                            segment_seconds=int(pcfg.get('segment_seconds', 2)),
                            transcode_audio=transcode_audio, pace_realtime=pace)
    log.info('Preview %s: channel %d (%s)%s - %s', session.id, channel.id, channel.name,
             ' with audio re-encoded to AAC' if transcode_audio else '',
             mask_creds_in_text(' '.join(cmd)))
    try:
        # stderr to a file, never a pipe: an undrained pipe once deadlocked every recording
        # in this app (dev/changelog/430), and the tail is what the modal shows on exit.
        with open(session.stderr_path, 'wb') as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err)
    except OSError as exc:
        detail = mask_creds_in_text(str(exc))
        log.error('Preview %s: could not start ffmpeg: %s', session.id, detail)
        _finish(session, REASON_LAUNCH_FAILED, detail=detail)
        raise PreviewRefused(f'Could not start ffmpeg: {detail}', 500)

    with _lock:
        session.proc = proc
        preempted = session.preempted
    if preempted:
        # A recording took the slot while ffmpeg was launching. Never run slotless.
        _finish(session, REASON_PREEMPTED)
        raise PreviewRefused(REASON_TEXT[REASON_PREEMPTED], 409)
    _ensure_reaper()
    return session


def _finish(session: PreviewSession, reason: str, detail: str = '') -> bool:
    """Tear down everything `session` acquired - ffmpeg, the connection slot, the segment
    directory - and only THEN mark it STOPPED with `reason`. The order is the contract:
    anything that reads STOPPED (the modal's poll, a Preview click that replaces this
    session and needs its slot back) can rely on the slot already being free. Idempotent:
    the first caller does the work and the rest block until it is done and return False,
    so the reaper, a Stop click and a preempting recording cannot fight over one session.
    Deliberately takes no database and no app context: run.py calls it from the shutdown
    signal handler."""
    with _lock:
        claimed = session.live and not session._finishing
        if claimed:
            session._finishing = True
            # Taken under _lock so no later caller can slip in ahead of the claimant.
            session._teardown_lock.acquire()
        proc = session.proc
    if not claimed:
        with session._teardown_lock:
            return False
    try:
        try:
            terminate_or_kill(proc)
        finally:
            try:
                connlim.release(session.account_id, 'preview', session.id)
            finally:
                if session.dir:
                    shutil.rmtree(session.dir, ignore_errors=True)
        with _lock:
            session.state = STATE_STOPPED
            session.reason = reason
            session.detail = detail
    finally:
        session._teardown_lock.release()
    level = logging.INFO if reason in (REASON_USER, REASON_REPLACED) else logging.WARNING
    log.log(level, 'Preview %s: channel %d (%s) stopped - %s%s', session.id,
            session.channel_id, session.channel_name, reason, f' ({detail})' if detail else '')
    return True


def stop_preview(session_id: str, reason: str = REASON_USER) -> bool:
    """Stop one session. False if it does not exist or had already stopped."""
    session = get_session(session_id)
    if session is None:
        return False
    return _finish(session, reason)


def stop_all(reason: str = REASON_SHUTDOWN) -> int:
    """Stop every live session (there is at most one). Returns how many were live."""
    with _lock:
        live = [s for s in _sessions.values() if s.live]
    for s in live:
        _finish(s, reason)
    return len(live)


def kill_all_previews():
    """Shutdown hook (run.py): stop the live preview so a restarted process never leaves an
    orphaned ffmpeg pulling a provider stream nobody is watching. No DB, signal-handler safe."""
    stop_all(REASON_SHUTDOWN)


def preempt_for_account(account_id: int) -> bool:
    """A recording on `account_id` needs the slot: stop the live preview if it is on that
    account. Called by recorder._try_acquire_slot_with_preemption after
    connection_limits.preempt_previews_for_slot() stripped the slot. Flags the session even
    when its ffmpeg is not registered yet, for the same reason
    channel_tester.kill_active_test_for_account does: a match with proc=None means the
    preview is inside its acquire-to-Popen window and must not launch on a stripped slot."""
    with _lock:
        session = _live_locked()
        if session is None or session.account_id != account_id:
            return False
        session.preempted = True
        launched = session.proc is not None
    if launched:
        _finish(session, REASON_PREEMPTED)
    return True


def touch_playlist(session_id: str) -> Optional[str]:
    """Path of the session's playlist for the route to serve, or None. A fetch is the
    player's heartbeat: it moves the idle clock, which is what lets a closed tab be
    noticed without the browser ever saying goodbye."""
    session = get_session(session_id)
    if session is None or not session.live:
        return None
    path = session.playlist_path
    if not os.path.exists(path):
        return None
    with _lock:
        session.last_seen_mono = time.monotonic()
        session.playlist_fetches += 1
    return path


def segment_path(session_id: str, name: str) -> Optional[str]:
    """Path of one segment file, or None. `name` must match exactly what ffmpeg names its
    segments - anything else (a traversal, the stderr spool, the playlist) is None."""
    if not PREVIEW_SEGMENT_RE.match(name or ''):
        return None
    session = get_session(session_id)
    if session is None or not session.live:
        return None
    path = os.path.join(session.dir, name)
    if not os.path.isfile(path):
        return None
    with _lock:
        session.last_seen_mono = time.monotonic()
    return path


def _playlist_is_playable(path: str) -> bool:
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            return '#EXTINF' in fh.read()
    except OSError:
        return False


def _check(session: PreviewSession):
    now = time.monotonic()
    proc = session.proc
    rc = proc.poll() if proc is not None else None
    if rc is not None:
        tail = mask_creds_in_text(read_stderr_tail(session.stderr_path))
        _finish(session, REASON_FFMPEG_EXITED,
                detail=f'ffmpeg exited with code {rc}' + (f': {tail}' if tail else ''))
        return
    if session.state == STATE_STARTING:
        if _playlist_is_playable(session.playlist_path):
            with _lock:
                if session.live:
                    session.state = STATE_READY
                    session.ready_mono = now
                    session.last_seen_mono = now
            log.info('Preview %s: playable after %.1fs', session.id, now - session.started_mono)
        elif now - session.started_mono > session.connect_timeout:
            tail = mask_creds_in_text(read_stderr_tail(session.stderr_path))
            _finish(session, REASON_CONNECT_TIMEOUT, detail=tail)
            return
    if session.state == STATE_READY and session.last_seen_mono is not None:
        if now - session.last_seen_mono > session.idle_timeout:
            _finish(session, REASON_IDLE)
            return
    if now - session.started_mono > session.max_seconds:
        _finish(session, REASON_MAX_DURATION)


def _reap_loop():
    global _reaper
    while True:
        time.sleep(_REAP_INTERVAL)
        with _lock:
            live = [s for s in _sessions.values() if s.live]
            if not live:
                # Exit and deregister under the SAME lock _ensure_reaper() checks, so a
                # session starting right now either sees this thread alive and about to
                # loop, or sees None and starts a new one. Never a live session with no reaper.
                _reaper = None
                return
        for s in live:
            _check(s)


def _ensure_reaper():
    global _reaper
    with _lock:
        if _reaper is not None:
            return
        _reaper = threading.Thread(target=_reap_loop, name='preview-reaper', daemon=True)
        _reaper.start()


def wait_for_reaper(timeout: float = 5.0) -> bool:
    """Block until the reaper thread has exited (no live session). True if it has."""
    with _lock:
        thread = _reaper
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def reset_for_tests():
    """Stop everything and forget every session. tests/support/app.py only."""
    stop_all(REASON_SHUTDOWN)
    wait_for_reaper()
    with _lock:
        _sessions.clear()
