"""The picture-in-picture engine: what puts a picture-in-picture channel's two pictures on a
TV as one (dev/docs/DESIGN-pip-channels.md sections 6, 7, 9 and 13).

A play of a set-up picture-in-picture channel is one output_feed.RelaySession as far as the TV
goes: one ring, one HTTP response, one line on Watching now, one player against "IPTV players
at once". Behind it:

* **Two pictures, owned by the channel's row and not by one play** (a Pair). Each Picture is
  an internal output_feed.RelaySession (`internal`, kept in output_feed._internal and listed
  nowhere) that reuses a plain play's seat, launch, stall detection, failover and move for a
  recording; its ffmpeg is build_relay_cmd()'s stream copy, and its bytes go into a
  PictureBuffer in place of a TV's ring. Two plays of one row, the channel and its swapped
  copy or two TVs on one channel, share the two pictures and so the two connections. A Pair
  with no play left lingers SWITCH_GRACE_SECONDS, because a player ends the old channel
  before it asks for the new one, and gives its seats up at once when anything asks
  (drop_lingering).
* **Per play, a Combined**: the combiner (proc_utils.build_pip_combiner_cmd), which is the
  session's own ffmpeg and is drained into its ring by the relay's reader; a writer thread
  from the big picture's buffer to the combiner's stdin; the small picture's decoder
  (build_pip_small_decoder_cmd) with a writer from the small picture's buffer and a reader
  that keeps the newest raw frame; and the pacer, which keeps the combiner's second pipe
  full with the newest frame, the last one again, or the small window's card after
  CARD_AFTER_SECONDS with none. The pacer has no clock (dev/changelog/1689).

The combiner is rebuilt, into the same ring behind a SOURCE_MARK, when the big picture's
source changes or the big window goes to or from a card. Nothing about the small picture
restarts it. Every pipe has one drain, every stderr is a file, and every child is killed
outright: an ffmpeg blocked writing to a pipe never acts on SIGTERM (dev/changelog/1689).

Locking: this module's state is guarded by output_feed._lock, held only for reads and
writes of fields, never across a spawn, a join, a database read or a pipe write. A rebuild
runs on a thread of its own under the play's `_swap_lock`.
"""
import json
import logging
import os
import secrets
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from functools import lru_cache
from typing import Optional

from . import connection_limits as connlim
from . import output_feed as feed
from . import pip_channels
from .database import FEED_PICTURE_BACK, FEED_PICTURE_STOPPED, FEED_PLAY_PICTURE
from .normalize import MATRIX_BT709, source_matrix
from .proc_utils import (build_pip_combiner_cmd, build_pip_small_decoder_cmd, pip_frame_bytes,
                         pip_geometry, read_stderr_tail, resolve_ffmpeg_path, terminate_or_kill)
from .url_utils import mask_creds_in_text

log = logging.getLogger(__name__)

#: A picture's buffer keeps this much, by arrival, and a new reader starts READER_BACK_SECONDS
#: back: with 6 s at hand an ffmpeg has its first frame out in 0.12 to 0.28 s, against 3.4 to
#: 4.9 s from the live edge (DESIGN-pip-channels.md section 2, item 6).
BUFFER_KEEP_SECONDS = 10.0
READER_BACK_SECONDS = 6.0
#: And never more than this many bytes. A provider opens a connection with 13 to 29 s of the
#: channel at once (dev/changelog/942, 997), which arrives faster than a combiner takes it, so
#: the cap has to hold a whole burst: 29 s at 17 Mbit/s. A reader the cap overtakes skips ahead
#: and says so in the log (pump).
BUFFER_MAX_BYTES = 64 * 1024 * 1024
#: A source is ready to be probed and read once it holds this much, or has been arriving this long.
READY_BYTES = 750_000
READY_SECONDS = 1.5
#: How much of a source ffprobe is shown to name its color matrix, height and interlacing.
PROBE_BYTES = 4 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 10
#: After this long with no frame from the decoder, the pacer writes the small window's card.
CARD_AFTER_SECONDS = 2.0
#: A combiner that dies this soon after it started, twice running, drops the play to the big
#: picture alone (section 7.3).
COMBINER_SETTLE_SECONDS = 15.0
#: A small picture's decoder that keeps dying is restarted at most this often, and given up
#: on for its source after this many deaths in a row.
DECODER_RETRY_SECONDS = 2.0
DECODER_MAX_DEATHS = 3

#: A live play of two pictures with no combiner for this long has nothing that could start one:
#: it is ended by name, since with no ffmpeg of its own the relay's idle check never sees it.
NO_COMBINER_SECONDS = 45.0
#: A rebuild that raises is tried again at the reaper's next turn, this many times in a row.
BUILD_MAX_FAILURES = 3

#: A big picture takes its small picture's connection at most this often: a channel that
#: will not start must not pause the small picture at every re-ask (section 8.2).
YIELD_AGAIN_SECONDS = 300.0
#: And walks at most this many of its own channels to see whether that would help.
YIELD_WALK = 8

#: What a combiner's big input and a decoder's input are, the first field of Combined.big_now
#: and small_now.
INPUT_LIVE = 'live'       # a picture's buffer, with its source number
INPUT_CARD = 'card'       # the big window's card, with what it says
INPUT_NONE = 'none'       # the small picture has no channel: no decoder

#: Handed out by a BufferReader when the bytes that follow come from another ffmpeg.
NEW_SOURCE = object()

#: The seams the suite's stand-in replaces (tests/support/pip_engine.py): the suite has no GPU.
build_combiner_cmd = build_pip_combiner_cmd
build_decoder_cmd = build_pip_small_decoder_cmd


# ── The picture buffer ─────────────────────────────────────────────────────────────────────

class PictureBuffer:
    """One picture's bytes, kept for BUFFER_KEEP_SECONDS and followed by any number of
    readers, each with its own place. It stands where a TV's Ring does on the picture's
    internal session, so it answers what the relay asks of a ring; nothing a reader does
    reaches the append, which is what keeps the picture's ffmpeg drained."""

    capacity = BUFFER_MAX_BYTES
    reserve = 0.0
    dropped = 0
    dropped_unripe = 0
    released_early = 0

    def __init__(self):
        self._chunks: deque = deque()      # (seq, arrived, source_no, data)
        self._next_seq = 0
        self.size = 0
        self.total_in = 0
        self.source_no = 0
        self._source_bytes = 0
        self._source_first: Optional[float] = None
        self._cv = threading.Condition()

    # What output_feed asks of a ring.
    def append(self, data: bytes, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._cv:
            self._chunks.append((self._next_seq, now, self.source_no, data))
            self._next_seq += 1
            self.size += len(data)
            self.total_in += len(data)
            self._source_bytes += len(data)
            if self._source_first is None:
                self._source_first = now
            while len(self._chunks) > 1 and (self.size > BUFFER_MAX_BYTES
                                             or self._chunks[0][1] < now - BUFFER_KEEP_SECONDS):
                self.size -= len(self._chunks.popleft()[3])
            self._cv.notify_all()

    def mark(self) -> None:
        """The bytes after this come from another ffmpeg: a new source."""
        with self._cv:
            self.source_no += 1
            self._source_bytes = 0
            self._source_first = None
            self._cv.notify_all()

    def clear(self) -> None:
        """A session that ended leaves its bytes for the readers still on them; they age out."""

    def wake(self) -> None:
        with self._cv:
            self._cv.notify_all()

    def set_reserve(self, reserve: float) -> None:
        """A picture's buffer has no reserve: the TV's ring keeps the play's."""

    def untaken_age(self, now: Optional[float] = None) -> float:
        return 0.0

    # What the engine asks of it.
    def ready(self, now: Optional[float] = None) -> bool:
        """Whether the current source holds enough to probe and to start an ffmpeg on."""
        now = time.monotonic() if now is None else now
        with self._cv:
            if self._source_first is None:
                return False
            return self._source_bytes >= READY_BYTES or now - self._source_first >= READY_SECONDS

    def snapshot(self, limit: int = PROBE_BYTES) -> bytes:
        """The start of what is held of the current source, at most `limit` bytes."""
        out, total = [], 0
        with self._cv:
            for _seq, _at, source_no, data in self._chunks:
                if source_no != self.source_no:
                    continue
                out.append(data)
                total += len(data)
                if total >= limit:
                    break
        return b''.join(out)[:limit]

    def reader(self, back: float = READER_BACK_SECONDS) -> 'BufferReader':
        """A reader of the current source, starting `back` seconds back or at the source's
        first byte still held, whichever is later."""
        now = time.monotonic()
        with self._cv:
            start = self._next_seq
            for seq, at, source_no, _data in self._chunks:
                if source_no == self.source_no and at >= now - back:
                    start = seq
                    break
            return BufferReader(self, start, self.source_no)


class BufferReader:
    """One reader's place in a PictureBuffer. `skipped` counts the times it fell so far
    behind that its place had aged out and it went on from the oldest byte held."""

    def __init__(self, buffer: PictureBuffer, seq: int, source_no: int):
        self.buffer = buffer
        self.seq = seq
        self.source_no = source_no
        self.skipped = 0

    def take(self, timeout: float):
        """The next chunk, NEW_SOURCE once the bytes that follow are another source's (and
        every time after: the reader is finished), or None after `timeout` with nothing new."""
        buf = self.buffer
        deadline = time.monotonic() + timeout
        with buf._cv:
            while True:
                if buf.source_no != self.source_no:
                    return NEW_SOURCE
                if buf._chunks and self.seq < buf._next_seq:
                    first = buf._chunks[0][0]
                    if self.seq < first:
                        self.seq = first
                        self.skipped += 1
                    seq, _at, source_no, data = buf._chunks[self.seq - first]
                    if source_no != self.source_no:
                        return NEW_SOURCE
                    self.seq = seq + 1
                    return data
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return None
                buf._cv.wait(wait)


# ── The plumbing between the ffmpegs ───────────────────────────────────────────────────────
#
# Module-level and free of any session, so the GPU lab's jobs drive the same three loops a
# play runs (dev/tools/gpu-lab/jobs/pip_lab.py).

def pump(reader: BufferReader, dst, stop: threading.Event) -> None:
    """Move one picture's bytes from its buffer to an ffmpeg's stdin until `stop`, then close
    the pipe. At a new source it stops writing and leaves the pipe open until `stop`: the
    owner rebuilds the ffmpeg, and an EOF here would read as its death."""
    skipped = 0
    try:
        while not stop.is_set():
            chunk = reader.take(0.5)
            if chunk is None:
                continue
            if chunk is NEW_SOURCE:
                stop.wait()
                break
            if reader.skipped != skipped:
                skipped = reader.skipped
                log.warning('Picture in picture: a reader fell behind its picture\'s buffer and skipped '
                            'ahead (time %d): the picture breaks up until its next keyframe', skipped)
            dst.write(chunk)
            dst.flush()
    except (OSError, ValueError):
        pass   # the ffmpeg is gone
    finally:
        try:
            dst.close()
        except (OSError, ValueError):
            pass   # gone with the process


def pace(fd: int, stop: threading.Event, next_frame) -> None:
    """Keep the combiner's second pipe full with whatever `next_frame(now)` returns, one
    blocking write after another, then close `fd`. No clock: the combiner takes
    geometry.small_fps frames for each second of the big picture by itself, so a big picture
    that arrives in a burst is combined as fast as it arrives (dev/changelog/1689)."""
    try:
        while not stop.is_set():
            view = memoryview(next_frame(time.monotonic()))
            while view:
                view = view[os.write(fd, view):]
    except OSError:
        pass   # the combiner is gone: its end of the pipe closed
    finally:
        try:
            os.close(fd)
        except OSError:
            pass   # already closed


def _join(threads) -> None:
    """Wait for threads that were started and are not this one. One stored and not yet
    started is skipped: its owner starts it with its stop already set, and it ends at once."""
    for t in threads:
        if t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5.0)


class NewestFrame:
    """The small picture's newest decoded frame: its decoder's stdout is drained here and
    everything but the last whole frame is dropped."""

    def __init__(self, frame_bytes: int):
        self.frame_bytes = frame_bytes
        self.frame: Optional[bytes] = None
        self.at = 0.0
        self.count = 0

    def read_from(self, src) -> None:
        """Drain `src` until it ends. The one drain of the decoder's stdout."""
        need, buf = self.frame_bytes, bytearray()
        try:
            while True:
                chunk = src.read(need - len(buf))
                if not chunk:
                    return
                buf += chunk
                if len(buf) == need:
                    self.frame, self.at, buf = bytes(buf), time.monotonic(), bytearray()
                    self.count += 1
        except (OSError, ValueError):
            return
        finally:
            try:
                src.close()
            except (OSError, ValueError):
                pass   # gone with the process

    def fresh(self, now: float) -> Optional[bytes]:
        """The newest frame while it is at most CARD_AFTER_SECONDS old, else None."""
        frame = self.frame
        return frame if frame is not None and now - self.at <= CARD_AFTER_SECONDS else None

    def clear(self) -> None:
        self.frame = None


# ── What a picture's stream is ─────────────────────────────────────────────────────────────

def probe_picture(cfg: dict, data: bytes) -> tuple:
    """(matrix, deinterlace) for a picture, read by ffprobe from bytes its buffer already holds:
    no connection. The driver does not read the color tag itself, and a wrong one shifts the
    colors by 33 of 255 (DESIGN-pip-channels.md section 2). Unreadable is bt709 by frame size
    unknown and not interlaced, said in the log."""
    ffprobe = (cfg.get('ffmpeg') or {}).get('ffprobe_path') or 'ffprobe'
    cmd = [ffprobe, '-v', 'error', '-f', 'mpegts', '-select_streams', 'v:0', '-show_entries',
           'stream=height,color_space,field_order', '-of', 'json', '-i', 'pipe:0']
    try:
        # run() drains both pipes itself (communicate), so neither can fill.
        done = subprocess.run(cmd, input=data, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS)
        stream = (json.loads(done.stdout or b'{}').get('streams') or [{}])[0]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        log.warning('Picture in picture: could not read a picture\'s format from its buffer (%s), '
                    'so it is taken as bt709 and not interlaced', exc)
        return MATRIX_BT709, False
    interlaced = str(stream.get('field_order') or '').lower() in ('tt', 'bb', 'tb', 'bt')
    return source_matrix(stream.get('color_space'), stream.get('height')), interlaced


# ── The GPU ────────────────────────────────────────────────────────────────────────────────

#: The last answer gpu_state() gave when it was no, for the page's notice: a page never runs a
#: trial itself (app/readiness.py: nothing expensive happens because someone looked).
_gpu_why = ''


def gpu_notice(cfg: dict) -> str:
    """Why a picture-in-picture channel plays its big picture alone right now, a sentence, or
    '': no GPU device, which costs a stat(), else what the last tune-in's trials found. No
    process is started."""
    pp = (cfg.get('recording') or {}).get('post_process') or {}
    device = str(pp.get('vaapi_device') or '').strip()
    if not device or not os.path.exists(device):
        where = f' at {device}' if device else ''
        return f'There is no GPU device{where} (recording.post_process.vaapi_device).'
    return _gpu_why


def gpu_state(cfg: dict) -> tuple:
    """(device, why not): _gpu_trials()'s answer, remembered for gpu_notice()."""
    global _gpu_why
    device, why = _gpu_trials(cfg)
    _gpu_why = why
    return device, why


def _gpu_trials(cfg: dict) -> tuple:
    """(device, '') when a picture-in-picture play may combine on the GPU, ('', why not as a
    sentence) when it may not: the render node must exist and pass both trials, the encode the
    conversion runs and the overlay of two pictures (section 7.1). A conversion running does
    not stop a play (section 7.3)."""
    from .toolchain import check_gpu_encoder, check_gpu_overlay
    pp = (cfg.get('recording') or {}).get('post_process') or {}
    device = str(pp.get('vaapi_device') or '').strip()
    if not device or not os.path.exists(device):
        where = f' at {device}' if device else ''
        return '', (f'Two pictures are combined only on a GPU, and there is no GPU device{where} '
                    '(recording.post_process.vaapi_device).')
    ffmpeg = resolve_ffmpeg_path(cfg['ffmpeg']['path'])
    trial = check_gpu_encoder(ffmpeg, device, 'h264', report=False)
    if not trial.ok:
        return '', f'A test encode through {device} failed: {trial.error}'
    trial = check_gpu_overlay(ffmpeg, device)
    if not trial.ok:
        return '', f'A test of combining two pictures through {device} failed: {trial.error}'
    return device, ''


# ── A small window's card ──────────────────────────────────────────────────────────────────

_CARD_BG = (18, 22, 30)
_CARD_FG = (236, 240, 246)
#: RGB to bt709 Y'CbCr in TV range, as the rows Image.convert() takes: what the combiner's
#: second input is pinned to (normalize.GPU_BT709_PIN).
_RGB_TO_BT709_TV = (
    0.2126 * 219 / 255, 0.7152 * 219 / 255, 0.0722 * 219 / 255, 16,
    -0.1146 * 224 / 255, -0.3854 * 224 / 255, 0.5 * 224 / 255, 128,
    0.5 * 224 / 255, -0.4542 * 224 / 255, -0.0458 * 224 / 255, 128)


@lru_cache(maxsize=64)
def card_frame(text: str, width: int, height: int) -> bytes:
    """One raw bt709 nv12 frame of width x height saying `text`: the small window's card, in
    the font the sports screens use."""
    from PIL import Image, ImageDraw
    from .sports_screen_draw import font
    img = Image.new('RGB', (width, height), _CARD_BG)
    draw = ImageDraw.Draw(img)
    face = font(700, max(14, height // 9))
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}'.strip()
        if line and draw.textlength(trial, font=face) > width * 0.86:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    block = '\n'.join(lines)
    box = draw.multiline_textbbox((0, 0), block, font=face, align='center', spacing=6)
    draw.multiline_text(((width - (box[2] - box[0])) / 2 - box[0], (height - (box[3] - box[1])) / 2 - box[1]),
                        block, font=face, fill=_CARD_FG, align='center', spacing=6)
    # Pillow's own matrix convert, so the card needs nothing the sports screens do not: the
    # three channels come out as Y, Cb and Cr.
    y, cb, cr = img.convert('RGB', _RGB_TO_BT709_TV).split()
    half = (width // 2, height // 2)
    chroma = Image.merge('LA', (cb.resize(half, Image.BOX), cr.resize(half, Image.BOX)))
    return y.tobytes() + chroma.tobytes()


# ── A picture, and the pair a row owns ─────────────────────────────────────────────────────

class Picture:
    """One of a row's two pictures: what it is right now (`pane`, a pip_channels.Pane), the
    internal session playing it while it has a channel, and the buffer its bytes go into.
    The buffer outlives any one session, so a picture that drops and comes back is the same
    buffer with a new source."""

    def __init__(self, pair: 'Pair', role: str):
        self.pair = pair
        self.role = role
        self.buffer = PictureBuffer()
        self.session: Optional[feed.RelaySession] = None
        self.pane = pip_channels.Pane(pip_channels.PANE_BUSY)
        # (the small window's card, Watching now's clause) for `pane`, worked out when the
        # pane is set and never per frame: a game's start time reads the display timezone.
        self.words = ('Waiting for a connection', 'waiting for a connection')
        # Moves on every pane write, under feed._lock: ended() names a stopped session's pane
        # after letting the lock go, and an ask landing in that gap has already named what is
        # next, which a stale "reconnecting" must not overwrite (dev/changelog/1715).
        self.pane_writes = 0
        self.failed_ids: set = set()
        self.seconds: dict = {}
        # Why a recording has its connection, '' while none has: what tells a picture a
        # recording took (PANE_TAKEN) from one that found every account busy (PANE_BUSY).
        self.taken_why = ''
        # The row's other picture took this one's connection (section 8.2), which its window
        # says while it waits; and when this picture last took the other's.
        self.yielded = False
        self.last_yield_mono: Optional[float] = None
        self.last_back_mono: Optional[float] = None
        # The game its last session was on, which a re-ask is held to as a failover is: a game
        # that is still on is never ended by the clock (dev/changelog/1685).
        self.event_id: Optional[int] = None
        self.last_ask_mono = 0.0
        self.ask_due = False
        self._asking = False
        # What the row said this picture is the last time it was asked (pip_channels.Source),
        # which a Save is compared against (row_changed), and the pane that ask resolved to.
        self.source = None
        self._resolved = self.pane

    @property
    def live(self) -> bool:
        s = self.session
        return s is not None and s.live

    @property
    def other(self) -> 'Picture':
        return self.pair.pictures[pip_channels.ROLE_INSET if self.role == pip_channels.ROLE_MAIN
                                  else pip_channels.ROLE_MAIN]

    def label(self) -> str:
        which = 'first' if self.role == pip_channels.ROLE_MAIN else 'second'
        return f'picture-in-picture channel {self.pair.row_id} ({self.pair.name}), its {which} picture'

    def resolve(self, cfg: dict, exclude_ids, event_id, viewer_id):
        """What this picture plays right now, as an output_feed.Play: the relay's re-ask at a
        failover, a move and a seat walk (output_feed._resolve_again), and the engine's own
        ask. The channel the row's other picture is on is never offered: two pictures of one
        channel would be one picture twice on two connections (section 5). Requires an app
        context."""
        from . import db
        from .database import PipChannel
        row = db.session.get(PipChannel, self.pair.row_id)
        source = self.source = pip_channels.source_of(row, self.role) if row is not None else None
        exclude = frozenset(exclude_ids)
        other = self.other.session
        other_id = other.channel_id if other is not None and other.live else None
        both = exclude | {other_id} if other_id is not None else exclude
        feed_id = feed.feed_id_for_pip(self.pair.row_id)
        pane = pip_channels.resolve_source(source, cfg, feed_id=feed_id, label=self.label(),
                                           exclude_ids=both, event_id=event_id, viewer_id=viewer_id)
        if (pane.state != pip_channels.PANE_LIVE and other_id is not None
                and self.role != self.pair.first_role):
            alone = pip_channels.resolve_source(source, cfg, feed_id=feed_id, label=self.label(),
                                                exclude_ids=exclude, event_id=event_id, viewer_id=viewer_id)
            if alone.state == pip_channels.PANE_LIVE and alone.play.member.id == other_id:
                pane.state, pane.why = pip_channels.PANE_SAME, 'it would be the same channel as the big picture'
        self._resolved = pane
        if pane.state == pip_channels.PANE_LIVE:
            return pane.play
        return feed.Play(feed_id, self.label(), why_none=pane.why or pane.state, fault=False)

    def ask(self, cfg: dict, wait: bool = False) -> None:
        """Ask what the picture plays and start it when it can: on the tune-in's own thread
        for the first ask (`wait` gives a seat the channel-change grace), on a thread of the
        engine's afterwards. Requires an app context. One ask at a time."""
        with feed._lock:
            if self._asking or self.pair.closed or self.live:
                return
            self._asking = True
            self.ask_due = False
            self.last_ask_mono = time.monotonic()
        try:
            # One picture of a pair at a time: each resolve leaves out the channel the other is
            # on, which two asks at once would both read as nothing.
            with self.pair.ask_lock:
                self._ask(cfg, wait)
        finally:
            with feed._lock:
                self._asking = False

    def _ask(self, cfg: dict, wait: bool, took: bool = False) -> None:
        from flask import current_app
        P = pip_channels
        play = self.resolve(cfg, self.failed_ids, self.event_id, None)
        pane = self._resolved
        if pane.state != P.PANE_LIVE:
            self._set_pane(pane)
            return
        member = play.member
        fcfg = feed.feed_config(cfg)
        limits = feed._session_limits(cfg, fcfg)
        limits.update(idle_timeout=float('inf'), max_seconds=float('inf'), reserve_seconds=0.0)
        session = feed.RelaySession(
            id=secrets.token_urlsafe(12), feed_id=feed.feed_id_for_pip(self.pair.row_id),
            label=f'{self.label()} ({pane.name})', client='', internal=True, picture=self,
            group_id=play.group.id if play.group is not None else None,
            channel_id=member.id, channel_name=member.name, account_id=member.account_id,
            ring=self.buffer, **limits, started_mono=time.monotonic(), event_id=play.event_id,
            fails_over=True, app=current_app._get_current_object(), preview=self.pair.preview)
        session.failed_ids = set(self.failed_ids)
        with feed._lock:
            if self.pair.closed:
                return
            feed._internal[session.id] = session
        deadline = time.monotonic() + (feed.SWITCH_GRACE_SECONDS if wait else 0.0)
        try:
            feed._launch(session, member, cfg, first=True, seat_deadline=deadline, hold=True)
        except feed._SeatHeld:
            self._forget(session)
            if not took and (self._take_small_seat(cfg) or self._take_seat_back(cfg)):
                self._ask(cfg, False, took=True)
                return
            with feed._lock:
                # Only while the picture that took its connection still plays on one.
                yielded = self.yielded = self.yielded and self.other.live
            if yielded:
                held = P.Pane(P.PANE_YIELDED, pane.source, pane.name, why='the big picture needed its connection')
            elif self.taken_why:
                held = P.Pane(P.PANE_TAKEN, pane.source, pane.name, why=self.taken_why)
            else:
                held = P.Pane(P.PANE_BUSY, pane.source, pane.name, why='every account it plays from is busy')
            self._set_pane(held)
            return
        except feed.FeedRefused as exc:
            self._forget(session)
            if session.reason == feed.REASON_PREEMPTED:
                # A recording took the seat inside the launch: the channel did not fail.
                self.taken_why = session.detail or 'a recording took the connection'
                self._set_pane(P.Pane(P.PANE_TAKEN, pane.source, pane.name, why=self.taken_why))
                return
            self.failed_ids.add(member.id)
            self._set_pane(P.Pane(P.PANE_CANNOT, pane.source, pane.name, why=exc.message.rstrip('.')))
            return
        except BaseException:
            # Whatever went wrong, a session left registered and live with no ffmpeg would
            # never be reaped, and would keep the seat it may have taken.
            feed._finish(session, feed.REASON_LAUNCH_FAILED)
            self._forget(session)
            raise
        with feed._lock:
            closed = self.pair.closed
            if not closed:
                self.session = session
                self.taken_why = ''
                self.yielded = False
                self.event_id = play.event_id
                self.pane, self.words = pane, ('', '')
                self.pane_writes += 1
        if closed:
            feed._finish(session, feed.REASON_CLIENT_GONE)
            self._forget(session)
            return
        feed._ensure_reaper()
        log.info('Picture in picture %s: %s plays channel %d (%s) on account %d', session.id,
                 self.label(), session.channel_id, session.channel_name, session.account_id)

    def _accounts(self, cfg: dict, viewer_id=None) -> set:
        """The accounts of the channels this picture could play right now, walking at most
        YIELD_WALK of them down its ranking. Requires an app context."""
        accounts, tried = set(), set(self.failed_ids)
        for _ in range(YIELD_WALK):
            member = self.resolve(cfg, tried, self.event_id, viewer_id).member
            if member is None:
                break
            accounts.add(member.account_id)
            tried.add(member.id)
        return accounts

    def _take_small_seat(self, cfg: dict) -> bool:
        """Every account this picture could play from is busy. When it is some TV's big
        picture and the row's other picture is nobody's, the other's connection is taken for
        it if it could play on that (section 8.2): the other is stopped, its window says why,
        and the caller asks again. False, with nothing touched, when that would not help.
        Requires an app context; the caller holds the pair's `ask_lock`."""
        now = time.monotonic()
        with feed._lock:
            other = self.other
            session = other.session
            bigs = self.pair.bigs_locked()
            ok = (self in bigs and other not in bigs and session is not None and session.live
                  and (self.last_yield_mono is None or now - self.last_yield_mono >= YIELD_AGAIN_SECONDS))
        if not ok:
            return False
        standing = connlim.seat_standing(self._accounts(cfg), 'viewer', exclude_holder=('viewer', session.id))
        if connlim.SEAT_FREE not in standing.values():
            return False
        with feed._lock:
            if other.session is not session or not session.live:
                return False
            other.yielded = True
            self.last_yield_mono = now
        log.warning('Picture in picture: %s has no connection free, so it takes the one %s is on '
                    '(account %s)', self.label(), other.label(), session.account_id)
        feed._finish(session, feed.REASON_STOPPED, detail='the big picture needed its connection')
        return True

    def _take_seat_back(self, cfg: dict) -> bool:
        """This picture gave its connection to the row's big picture and is still waiting for
        one. When the big picture could now play from another account that has a connection
        free, it is stopped so that it moves there, and the caller asks again and has its own
        connection back (section 8.2). The big picture reconnects once for it, and if where
        it went does not start it takes this connection again. At most once in
        YIELD_AGAIN_SECONDS. False, with nothing touched, when the big picture has nowhere
        else to go. Requires an app context;
        the caller holds the pair's `ask_lock`."""
        now = time.monotonic()
        with feed._lock:
            other = self.other
            session = other.session
            ok = (self.yielded and session is not None and session.live and not session._moving
                  and (self.last_back_mono is None or now - self.last_back_mono >= YIELD_AGAIN_SECONDS))
        if not ok:
            return False
        mine = connlim.seat_standing(self._accounts(cfg), 'viewer', exclude_holder=('viewer', session.id))
        if connlim.SEAT_FREE not in mine.values():
            return False
        elsewhere = other._accounts(cfg, session.id) - {session.account_id}
        if connlim.SEAT_FREE not in connlim.seat_standing(elsewhere, 'viewer').values():
            return False
        with feed._lock:
            if other.session is not session or not session.live:
                return False
            # Once in YIELD_AGAIN_SECONDS, and the big picture may take the connection again at
            # once: where it moves to may not start, and it must not sit on a card for that.
            self.last_back_mono = now
            other.last_yield_mono = None
        log.warning('Picture in picture: a connection is free again, so %s moves to it and %s has '
                    'its own back (account %s)', other.label(), self.label(), session.account_id)
        feed._finish(session, feed.REASON_STOPPED, detail='moving to a free connection so the small '
                                                          'picture has its own back')
        return True

    def _forget(self, session) -> None:
        with feed._lock:
            feed._internal.pop(session.id, None)

    def _set_pane(self, pane, unless_after: Optional[int] = None) -> None:
        """Name the pane. With `unless_after`, a count of pane_writes read earlier, only when
        nothing has been written since."""
        words = pip_channels.pane_words(pane)
        with feed._lock:
            if unless_after is not None and self.pane_writes != unless_after:
                return
            changed = (pane.state, pane.why) != (self.pane.state, self.pane.why)
            self.pane, self.words = pane, words
            self.pane_writes += 1
        if changed:
            log.info('Picture in picture: %s is not playing (%s%s)', self.label(), pane.state,
                     f': {pane.why}' if pane.why else '')

    def ended(self) -> bool:
        """The reaper's look at a picture whose session has stopped: keep what it learned, name
        the pane, and ask again at once (a preemption or a drop usually has somewhere to go).
        True when a session was collected. Caller does not hold the lock."""
        P = pip_channels
        with feed._lock:
            session = self.session
            if session is None or session.live:
                return False
            self.session = None
            feed._internal.pop(session.id, None)
            self.failed_ids |= session.failed_ids
            for account_id, seconds in session.account_seconds.items():
                self.seconds[account_id] = self.seconds.get(account_id, 0.0) + seconds
            name, source = self.pane.name, self.pane.source
            reason = session.reason
            self.ask_due = True
            yielded = self.yielded
            writes = self.pane_writes
        if yielded:
            pane = P.Pane(P.PANE_YIELDED, source, name, why='the big picture needed its connection')
        elif reason == feed.REASON_PREEMPTED:
            self.taken_why = session.detail or 'a recording took the connection'
            pane = P.Pane(P.PANE_TAKEN, source, name, why=self.taken_why)
        elif reason == feed.REASON_ACCOUNT_OFF:
            pane = P.Pane(P.PANE_CANNOT, source, name, why='the account it was playing from was turned off')
        elif reason in _DROPPED_REASONS:
            pane = P.Pane(P.PANE_RECONNECTING, source, name, why=session.detail)
        elif reason in _CLOSED_REASONS:
            # Its pair is closing, or its source was changed (row_changed): the ask that
            # follows names what it is next.
            pane = P.Pane(P.PANE_RECONNECTING, source, name, why='its picture is changing')
        else:
            # Not an end a picture's session is known to reach. The pane must stop saying it
            # plays, so it is named as dropped, and the log says what was not expected.
            log.error('Picture in picture: %s stopped for a reason a picture is not expected to (%s)',
                      self.label(), reason)
            pane = P.Pane(P.PANE_RECONNECTING, source, name, why=session.detail)
        self._set_pane(pane, unless_after=writes)
        log.warning('Picture in picture: %s stopped playing "%s" (%s%s)', self.label(),
                    session.channel_name, session.reason, f': {session.detail}' if session.detail else '')
        return True

    def claim_seconds(self) -> dict:
        """The provider time this picture has run up since it was last claimed, by account:
        credited to the play that ends, once (output_feed._finish)."""
        with feed._lock:
            session = self.session
            if session is not None and session._phase is not None:
                proc, account_id, _started = session._phase
                feed._close_phase_locked(session)
                session._phase = (proc, account_id, time.monotonic())
            if session is not None:
                for account_id, seconds in session.account_seconds.items():
                    self.seconds[account_id] = self.seconds.get(account_id, 0.0) + seconds
                session.account_seconds = {}
            out, self.seconds = self.seconds, {}
        return out

    def view(self) -> dict:
        """What Watching now says of the picture."""
        with feed._lock:
            session, pane = self.session, self.pane
            if session is not None and session.live:
                return {'live': True, 'name': pane.name, 'channel_id': session.channel_id,
                        'channel_name': session.channel_name, 'account_id': session.account_id,
                        'text': '', 'moves': len(session.failovers)}
            text = self.words[1]
        return {'live': False, 'name': pane.name, 'channel_id': None, 'channel_name': '',
                'account_id': None, 'text': text, 'moves': 0}


#: Why a picture's session stops when its channel stopped delivering or would not start.
_DROPPED_REASONS = (feed.REASON_NO_MEMBER, feed.REASON_FFMPEG_EXITED, feed.REASON_CONNECT_TIMEOUT,
                    feed.REASON_NO_DATA, feed.REASON_TOO_SLOW, feed.REASON_LAUNCH_FAILED)
#: And when the engine or a person closed it.
_CLOSED_REASONS = (feed.REASON_STOPPED, feed.REASON_TAKEN_OFF, feed.REASON_CLIENT_GONE,
                   feed.REASON_SHUTDOWN, feed.REASON_DISABLED)


class Pair:
    """A row's two pictures and the plays on them."""

    def __init__(self, row_id: int, name: str, first_role: str, app, preview: bool):
        self.row_id = row_id
        self.name = name
        self.first_role = first_role
        self.app = app
        self.preview = preview
        self.pictures = {role: Picture(self, role) for role in pip_channels.ROLES}
        # {a play's session id: whether it is of the swapped copy}
        self.plays: dict = {}
        self.idle_since: Optional[float] = None
        self.closed = False
        self.ask_lock = threading.Lock()
        # The layout the last Save gave the row (row_changed), None before one: a play that
        # attaches after the Save listed the row's plays takes it here (Combined.attach_locked).
        self.geometry = None

    def ordered(self) -> list:
        return sorted(self.pictures.values(), key=lambda p: p.role != self.first_role)

    def bigs_locked(self) -> set:
        """The pictures some play has as its big one. With the channel and its swapped copy
        both watched that is both of them. The caller holds output_feed._lock."""
        P = pip_channels
        return {self.pictures[P.ROLE_INSET if swapped else P.ROLE_MAIN] for swapped in self.plays.values()}


_pairs: dict = {}


def _acquire(row_id: int, name: str, swapped: bool, session, app) -> Pair:
    first = pip_channels.ROLE_INSET if swapped else pip_channels.ROLE_MAIN
    with feed._lock:
        pair = _pairs.get(row_id)
        if pair is None or pair.closed:
            pair = _pairs[row_id] = Pair(row_id, name, first, app, session.preview)
        pair.plays[session.id] = swapped
        pair.idle_since = None
    return pair


def _release(pair: Pair, session) -> None:
    with feed._lock:
        pair.plays.pop(session.id, None)
        if not pair.plays and pair.idle_since is None:
            pair.idle_since = time.monotonic()


def _close_pair(pair: Pair, reason: str, even_if_played: bool = False) -> bool:
    """Close a pair's two pictures: their ffmpegs killed, seats released, readers joined
    (output_feed._finish), and the pair forgotten. Only a pair no play is on, unless
    `even_if_played`: "no play is on it" is decided here, in the hold of the lock that marks it
    closed, so a play that joined a moment ago (_acquire) is never left on a closed pair with
    nothing to combine. False when it was left open."""
    with feed._lock:
        if pair.closed or (pair.plays and not even_if_played):
            return False
        pair.closed = True
        if _pairs.get(pair.row_id) is pair:
            del _pairs[pair.row_id]
        sessions = [p.session for p in pair.pictures.values() if p.session is not None]
    for s in sessions:
        feed._finish(s, reason)
        with feed._lock:
            feed._internal.pop(s.id, None)
    return True


def drop_lingering() -> int:
    """Close every pair no play is on, now: something asked for a connection, and a lingering
    picture never keeps one from it (section 9). How many pairs it closed."""
    with feed._lock:
        idle = [p for p in _pairs.values() if not p.plays]
    return sum(1 for pair in idle if _close_pair(pair, feed.REASON_CLIENT_GONE))


def close_row(row_id: int, reason: str = feed.REASON_TAKEN_OFF) -> None:
    """Close a row's pictures now, lingering or not: the channel was taken off or removed."""
    with feed._lock:
        pair = _pairs.get(row_id)
    if pair is not None:
        _close_pair(pair, reason)


def close_all(reason: str = feed.REASON_SHUTDOWN) -> None:
    with feed._lock:
        pairs = list(_pairs.values())
    for pair in pairs:
        _close_pair(pair, reason, even_if_played=True)


def give_way() -> dict:
    """{a picture's seat holder id: connection_limits.VIEWER_*}: how readily each picture
    gives its connection to a recording (section 8.2). A picture no play is on goes first, then
    one that is the small picture for every play on it. One that is big for any play is as
    watched as a plain play."""
    out = {}
    with feed._lock:
        for session in feed._internal.values():
            picture = session.picture
            if picture is None or not session.live:
                continue
            pair = picture.pair
            out[session.id] = (connlim.VIEWER_UNWATCHED if not pair.plays else
                               connlim.VIEWER_WATCHED if picture in pair.bigs_locked() else
                               connlim.VIEWER_SMALL_PICTURE)
    return out


def picture_accounts() -> list:
    """The account each playing picture holds a connection on, one entry per picture: what
    the IPTV Output page's Accounts card counts beside the plain plays (section 8.1). Two
    plays of one row share its pictures, so they are counted once."""
    with feed._lock:
        return [s.account_id for s in feed._internal.values()
                if s.live and s.picture is not None and s.account_id is not None]


def _combos_locked(pair: Pair) -> list:
    """The live plays on `pair` combined. The caller holds output_feed._lock."""
    return [s.combined for s in feed._sessions.values()
            if s.live and s.combined is not None and s.combined.pair is pair]


def _swap_roles(pair: Pair, row) -> bool:
    """When the row's two pictures were exchanged and nothing else about them changed, the
    pair's two pictures change places and each play's windows with them, on the connections
    they already hold (section 9). False, with nothing touched, for any other change. The
    caller holds the pair's `ask_lock`, so no ask reads a role half way through. The plays are
    listed in the hold that swaps the pair: one that attaches later reads the roles from the
    pair (Combined.attach_locked)."""
    P = pip_channels
    main, inset = pair.pictures[P.ROLE_MAIN], pair.pictures[P.ROLE_INSET]
    want_main, want_inset = P.source_of(row, P.ROLE_MAIN), P.source_of(row, P.ROLE_INSET)
    if (main.source is None or inset.source is None or want_main == want_inset
            or want_main != inset.source or want_inset != main.source):
        return False
    with feed._lock:
        if pair.closed:
            return False
        pair.pictures = {P.ROLE_MAIN: inset, P.ROLE_INSET: main}
        main.role, inset.role = P.ROLE_INSET, P.ROLE_MAIN
        pair.first_role = P.ROLE_INSET if pair.first_role == P.ROLE_MAIN else P.ROLE_MAIN
        for picture in (main, inset):
            # One that gave its connection to the other may now be the big picture.
            picture.ask_due = picture.ask_due or not picture.live
        for combined in _combos_locked(pair):
            combined.swap_roles_locked()
    log.info('Picture in picture: channel %d (%s) had its two pictures swapped, on the '
             'connections they hold', pair.row_id, pair.name)
    return True


def row_changed(row_id: int) -> int:
    """A picture-in-picture channel's pictures or layout were saved while it may be watched:
    a picture whose source changed is stopped and asks again at once, one that did not change
    keeps its connection, and every play of the row takes the new layout
    (dev/docs/DESIGN-pip-channels.md 10). Two pictures that only changed places keep both
    connections (section 9). The combiner follows by itself: a new source or a
    card in the big window restarts it. How many plays are on the row's two pictures. Requires
    an app context; called after the save's commit."""
    from . import db
    from .database import PipChannel
    with feed._lock:
        pair = _pairs.get(row_id)
    if pair is None:
        return 0
    row = db.session.get(PipChannel, row_id)
    if row is None:
        return 0
    geometry = pip_geometry(row.layout, row.corner, row.size)
    stopping = []
    with pair.ask_lock:
        with feed._lock:
            # Before the plays are listed below: a play that attaches in between takes it
            # (dev/docs/BUGS.md 2026-11-25 @ 07:56:12 PM).
            pair.geometry = geometry
        _swap_roles(pair, row)
        for picture in list(pair.pictures.values()):
            if pip_channels.source_of(row, picture.role) == picture.source:
                continue
            with feed._lock:
                session = picture.session
                picture.failed_ids.clear()
                picture.event_id = None
                picture.taken_why = ''
                picture.yielded = False
                picture.ask_due = True
            if session is not None and session.live:
                stopping.append(session)
    for session in stopping:
        feed._finish(session, feed.REASON_STOPPED, detail='its picture was changed')
    with feed._lock:
        combos = _combos_locked(pair)
    for combined in combos:
        combined.set_geometry(geometry)
    return len(combos)


def ask_all() -> None:
    """A connection or a slot came free: every picture that is waiting asks again at the
    reaper's next turn."""
    with feed._lock:
        for pair in _pairs.values():
            for picture in pair.pictures.values():
                if not picture.live:
                    picture.ask_due = True


def idle_locked() -> bool:
    """Whether no row has pictures: the reaper may stop. The caller holds output_feed._lock."""
    return not _pairs


def reset_for_tests() -> None:
    close_all()
    global _gpu_why
    with feed._lock:
        _pairs.clear()
    _gpu_why = ''
    card_frame.cache_clear()


# ── One play's combined picture ────────────────────────────────────────────────────────────

class Combined:
    """What one TV's play of a picture-in-picture channel runs beside its session: see the
    module's docstring. `big` and `small` are the pair's pictures in this play's roles."""

    def __init__(self, session, pair: Pair, swapped: bool, geometry, device: str, cfg: dict):
        roles = (pip_channels.ROLE_INSET, pip_channels.ROLE_MAIN) if swapped else pip_channels.ROLES
        self.session = session
        self.pair = pair
        self.swapped = swapped
        self.big = pair.pictures[roles[0]]
        self.small = pair.pictures[roles[1]]
        self.geometry = geometry
        self.device = device
        self.cfg = cfg
        self.frame_bytes = pip_frame_bytes(geometry)
        self.closed = False
        self._busy = False
        self.build_failures = 0
        self._no_combiner_since = 0.0
        # The combiner: what its big input is now, (INPUT_LIVE, source_no) or (INPUT_CARD, ...).
        self.big_now: Optional[tuple] = None
        self.big_gpu = True
        self.combiner_started = 0.0
        self.deaths = 0
        self._big_stop: Optional[threading.Event] = None
        self._threads: list = []
        # The small picture's decoder.
        self.small_now: Optional[tuple] = None
        self.small_gpu = True
        self.decoder: Optional[subprocess.Popen] = None
        self.decoder_stderr = ''
        self.decoder_deaths = 0
        self.decoder_died_at = 0.0
        self._decoder_threads: list = []
        self._small_stop: Optional[threading.Event] = None
        self.frames = NewestFrame(self.frame_bytes)
        # Counted for tests: frames written, of which cards.
        self.written = self.cards = 0
        # Whether the small picture was playing at the last look, None before the first: what
        # Recent plays' lines for it stopping and coming back are written from.
        self._small_live: Optional[bool] = None
        # Counts the times the play's windows changed under it (a swap, a new layout). A
        # rebuild that started before one records nothing as built, so the next turn builds
        # again to what the windows are now.
        self._shape = 0
        # (session.last_data_mono, the big picture's buffer.total_in, when that buffer was
        # first seen to grow past it) as of the combiner's output last moving: what
        # silent_for() measures from.
        self._out_mark: tuple = (None, 0, None)

    @property
    def had_frame(self) -> bool:
        return self.frames.count > 0

    # -- what the pacer writes ---------------------------------------------------------------

    def _frame(self, now: float) -> bytes:
        self.written += 1
        frame = self.frames.fresh(now)
        if frame is not None:
            return frame
        self.cards += 1
        g = self.geometry
        return card_frame(self._small_words(), g.small_w, g.small_h)

    def _small_words(self) -> str:
        with feed._lock:
            words, live = self.small.words[0], self.small.live
        if self.decoder_deaths >= DECODER_MAX_DEATHS:
            return 'Cannot be shown'
        if live or not words:
            # It has a channel and no frame to show: starting, or its stream stopped arriving.
            return 'Reconnecting' if self.had_frame else 'Starting'
        return words

    # -- the reaper's turn -------------------------------------------------------------------

    def _want_big(self) -> Optional[tuple]:
        """What the combiner's big input should be: ('live', source_no) once the big picture's
        current source is ready, ('card', words) when it has no channel, None to leave things
        as they are (a picture between channels: the TV holds, as a plain play's does)."""
        big = self.big
        with feed._lock:
            live, pane = big.live, big.pane
        if live:
            return (INPUT_LIVE, big.buffer.source_no) if big.buffer.ready() else None
        if pane.state == pip_channels.PANE_LIVE:
            return None     # its session just stopped and the reaper has not named why yet
        return (INPUT_CARD, pane.state, pane.name, pane.why)

    def _want_small(self) -> Optional[tuple]:
        small = self.small
        with feed._lock:
            live = small.live
        if not live:
            return (INPUT_NONE,)
        return (INPUT_LIVE, small.buffer.source_no) if small.buffer.ready() else self.small_now

    def tick(self, now: float) -> None:
        session = self.session
        with feed._lock:
            if self.closed or not session.live or session._finishing:
                return
            orphaned = self.pair.closed
            if session.proc is not None:
                self._no_combiner_since = 0.0
            elif not self._no_combiner_since:
                self._no_combiner_since = now
            starved = bool(self._no_combiner_since) and now - self._no_combiner_since > NO_COMBINER_SECONDS
            busy = self._busy
            decoder = self.decoder
        if orphaned or starved:
            # Nothing would ever start a combiner for it, and with no ffmpeg of its own the
            # relay's checks pass it by: it is ended by name, and the player tunes in again.
            feed._finish(session, feed.REASON_LAUNCH_FAILED,
                         detail=('its pictures were closed under it' if orphaned else
                                 f'no picture could be combined in {NO_COMBINER_SECONDS:.0f}s'))
            return
        self._note_small()
        if busy:
            return
        want_big = self._want_big()
        want_small = self._want_small()
        job = None
        if want_big is not None and want_big != self.big_now:
            # Both at once at a tune-in and at a swap, so the small window is not a turn behind.
            both = want_small is not None and want_small != self.small_now
            job = (self._rebuild_both, (want_big, want_small)) if both else (self._rebuild_combiner, (want_big, ''))
        elif want_small != self.small_now and want_small is not None:
            job = (self._rebuild_decoder, (want_small,))
        elif (decoder is not None and decoder.poll() is not None and self.small_now is not None
              and self.small_now[0] == INPUT_LIVE and now - self.decoder_died_at >= DECODER_RETRY_SECONDS):
            job = (self._decoder_died, (decoder,))
        if job is None:
            return
        self._start_job(*job)

    def _rebuild_both(self, want_big: tuple, want_small: tuple) -> None:
        self._rebuild_combiner(want_big, '')
        if not self.closed and self.session.live:
            self._rebuild_decoder(want_small)

    def silent_for(self, now: float) -> Optional[float]:
        """How long the combiner has sent nothing while its big picture kept arriving, or None
        when that is not what is happening: it has sent nothing yet (the connect timeout's), the
        big window is on its card, or the big picture is between sources, where the pump holds
        the pipe open on purpose and the combiner waits for its rebuild. A big picture that
        stopped arriving is its own session's stall, never the combiner's, so the silence runs
        from the later of the last output and the big picture arriving again after it: a source
        that paused and came back gives the combiner the whole timeout to answer."""
        session = self.session
        with feed._lock:
            last, big, big_now = session.last_data_mono, self.big, self.big_now
            live = big.live
        if last is None:
            return None
        total = big.buffer.total_in
        mark_last, mark_total, grew_at = self._out_mark
        if mark_last != last:
            self._out_mark = (last, total, None)
            return None
        if (not live or big_now is None or big_now[0] != INPUT_LIVE
                or big_now[1] != big.buffer.source_no or total <= mark_total):
            return None
        if grew_at is None:
            grew_at = now
            self._out_mark = (mark_last, mark_total, grew_at)
        return now - max(last, grew_at)

    def swap_roles_locked(self) -> None:
        """The row's two pictures changed places: this play's two windows exchange what they
        show, and the reaper's next turn builds the combiner and the decoder to it. The frames
        in hand are the other picture's, so the small window shows its card until the new
        decoder's first frame. The caller holds output_feed._lock."""
        if self.closed:
            return
        self.big, self.small = self.small, self.big
        self.frames = NewestFrame(self.frame_bytes)
        self.big_now = self.small_now = None
        self._shape += 1
        self._small_live = None     # a swap is not the small picture stopping

    def _note_small(self) -> None:
        """A line in Recent plays when this play's small picture stops and when it is back
        (section 12), so an evening reads as what happened. Nothing for how it was at the
        first look: the play's own line names both pictures."""
        session = self.session
        with feed._lock:
            small = self.small
            live, was = small.live, self._small_live
            if live == was or (not live and small.pane.state == pip_channels.PANE_LIVE):
                return      # no change, or stopped and the reaper has not named why yet
            self._small_live = live
            name, why, playing = small.pane.name, small.words[1], small.session
        if was is None:
            return
        now = datetime.utcnow()
        entry = {'kind': FEED_PLAY_PICTURE, 'at': now, 'first_at': now, 'feed_id': session.feed_id,
                 'label': session.label, 'client': session.client}
        if live:
            entry.update(reason=FEED_PICTURE_BACK, channel_id=playing.channel_id,
                         channel_name=playing.channel_name, account_id=playing.account_id,
                         message=f'The small picture ({name}) is back.')
        else:
            entry.update(reason=FEED_PICTURE_STOPPED,
                         message=f'The small picture ({name}) stopped: {why or "it has no channel"}.')
        feed._queue_play(session.app, entry)

    def _start_job(self, fn, args) -> bool:
        with feed._lock:
            if self._busy or self.closed:
                return False
            self._busy = True

        def run():
            session = self.session
            try:
                with self.pair.app.app_context(), session._swap_lock:
                    fn(*args)
                self.build_failures = 0
            except Exception as exc:                    # noqa: BLE001 - one play's rebuild, never the reaper
                log.exception('Viewer %s: %s could not rebuild its picture', session.id, session.label)
                self.build_failures += 1
                with feed._lock:
                    left = session.combined is None and session.proc is None
                if left or self.build_failures >= BUILD_MAX_FAILURES:
                    # It has left its pictures and has no ffmpeg, or the rebuild keeps failing:
                    # nothing else would end it, so it ends here, by name.
                    feed._finish(session, feed.REASON_LAUNCH_FAILED,
                                 detail=f'its picture could not be rebuilt: {mask_creds_in_text(str(exc))[:200]}')
            finally:
                with feed._lock:
                    self._busy = False

        threading.Thread(target=run, daemon=True, name=f'pip-build-{self.session.id}').start()
        return True

    # -- the combiner ------------------------------------------------------------------------

    def _stop_combiner(self) -> None:
        feed._stop_current(self.session)
        with feed._lock:
            threads, self._threads = self._threads, []
            stop, self._big_stop = self._big_stop, None
        if stop is not None:
            stop.set()
        _join(threads)

    def _rebuild_combiner(self, want: tuple, why: str) -> None:
        """Stop the combiner and start one on `want`, into the same ring behind a SOURCE_MARK:
        the TV sees the MPEG-TS discontinuity a failover sends."""
        session = self.session
        with feed._lock:
            shape, g, big = self._shape, self.geometry, self.big
        self._stop_combiner()
        if self.closed or not session.live or session._finishing:
            return
        reader = None
        clip = ''
        if want[0] == INPUT_LIVE:
            matrix, deinterlace = probe_picture(self.cfg, big.buffer.snapshot())
            kw = {'gpu_decode': self.big_gpu, 'deinterlace': deinterlace, 'matrix': matrix}
            reader = big.buffer.reader()
        elif want[0] == INPUT_CARD:
            clip = self._big_card()
            if not clip:
                return
            kw = {'big_input': clip}
        else:
            raise ValueError(f'Unknown big input "{want[0]}"')
        rd, wr = os.pipe()
        try:
            cmd = build_combiner_cmd(self.cfg, self.device, g, small_input=f'pipe:{rd}', **kw)
        except BaseException:
            os.close(rd)
            os.close(wr)
            raise
        log.info('Viewer %s: combining %s%s: %s', session.id, 'its two pictures' if reader else 'its card and its small picture',
                 f' ({why})' if why else '', mask_creds_in_text(' '.join(cmd)))
        try:
            fd, session.stderr_path = tempfile.mkstemp(prefix='channelbin-pip-', suffix='.stderr')
            os.close(fd)
            # stderr to a file, never a pipe (dev/changelog/430). stdout is drained by the
            # relay's reader, stdin fed by _pump, the second pipe kept full by _pace.
            with open(session.stderr_path, 'wb') as err:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if reader else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=err, pass_fds=(rd,))
        except OSError as exc:
            os.close(rd)
            os.close(wr)
            feed._remove_stderr(session)
            self._go_alone(f'the combining ffmpeg could not start: {exc}')
            return
        os.close(rd)
        stop = threading.Event()
        threads = [threading.Thread(target=pace, args=(wr, stop, self._frame), daemon=True,
                                    name=f'pip-pace-{session.id}')]
        if reader is not None:
            threads.append(threading.Thread(target=pump, args=(reader, proc.stdin, stop),
                                            daemon=True, name=f'pip-big-{session.id}'))
        # Its threads are the play's to stop from here on, before they or the combiner start:
        # a close() that lands in between sets `stop`, and each thread then ends at once and
        # closes its own pipe.
        with feed._lock:
            dead = self.closed
            if not dead:
                self._threads, self._big_stop = threads, stop
                session.state = feed.STATE_STARTING
                session.last_data_mono = None
                session.screen_path = clip
        if dead or feed._adopt(session, proc) is None:
            # The play ended while the combiner was starting (_adopt killed it, or it is killed
            # here). The threads still run, to close the two pipes they own.
            stop.set()
            if dead:
                terminate_or_kill(proc, hard=True)
                if proc.stdout is not None:
                    proc.stdout.close()
                feed._remove_stderr(session)
        for t in threads:
            t.start()
        if stop.is_set():
            return
        with feed._lock:
            # None when the windows changed while this was building: the next turn builds again.
            self.big_now = want if self._shape == shape else None
            self.combiner_started = time.monotonic()

    def _big_card(self) -> str:
        """The clip the big window shows while its picture has no channel: the channel's card,
        drawn as a play on its card draws it. '' when it could not be made, the play then
        ended by name."""
        from . import sports_screen as ss
        P = pip_channels
        session = self.session
        with feed._lock:
            pane = self.big.pane
        if pane.state in (P.PANE_SAME, P.PANE_YIELDED):
            # States a row's second picture is given, which the channel's card has no words
            # for. It is the big picture for this play (the swapped copy), so the card says
            # it cannot play and why.
            why = ('it would be the same channel as the other picture' if pane.state == P.PANE_SAME
                   else 'the other picture needed its connection')
            pane = P.Pane(P.PANE_CANNOT, pane.source, pane.name, why=why)
        tuned = P.Tuned(self.pair.row_id, self.pair.name, self.swapped, True, pane)
        held = ss.HELD_NO_CONNECTION if pane.state == P.PANE_BUSY else None
        try:
            card = P.card_for(tuned, self.cfg, held=held)
            path, _still = feed._clip_for(self.cfg, ss.pip_owner(self.pair.row_id), card.screen)
        except ss.ClipFailed as exc:
            feed._finish(session, feed.REASON_CARD_FAILED, detail=f'the big picture has nothing to play, '
                                                                  f'and its card could not be shown: {exc}')
            return ''
        return path

    def combiner_died(self, proc, why: str, worked_until: Optional[float] = None) -> None:
        """The reaper found the combiner dead, or silent past the connect timeout
        (output_feed._check). One restart decoding the big picture on the CPU when its GPU
        decode is what failed; otherwise one restart, and the big picture alone when that one
        dies in its first seconds too (section 7.3). `worked_until` is when a combiner that
        hung last sent anything: its first seconds are judged by that, not by when the hang
        was noticed a stall timeout later, so one that wedges after a few frames every time
        still ends on the big picture alone."""
        from .preview import gpu_decode_failure
        session = self.session
        blamed = (gpu_decode_failure(session.stderr_path) if self.big_gpu and self.big_now
                  and self.big_now[0] == INPUT_LIVE else '')
        tail = mask_creds_in_text(read_stderr_tail(session.stderr_path))
        detail = why + (f': {tail}' if tail else '')

        def job():
            with feed._lock:
                if session.proc is not proc:
                    return   # already rebuilt for another reason
            want = self._want_big() or self.big_now
            if blamed:
                self.big_gpu = False
                name = self.big.pane.name or 'the big picture'
                feed._note_once(session, f'The GPU could not decode {name}, so it is decoded on the CPU '
                                         f'for this play.')
                log.warning('Viewer %s: the GPU could not decode the big picture (%s), so the combiner '
                            'restarts decoding it on the CPU', session.id, blamed)
                self._rebuild_combiner(want, 'its big picture decoded on the CPU')
                return
            ended = worked_until if worked_until is not None else time.monotonic()
            quick = ended - self.combiner_started < COMBINER_SETTLE_SECONDS
            self.deaths = self.deaths + 1 if quick else 1
            log.warning('Viewer %s: the combiner stopped (%s), time %d', session.id, detail, self.deaths)
            if self.deaths >= 2:
                self._go_alone(detail)
                return
            self._rebuild_combiner(want, 'restarted after it stopped')

        self._start_job(job, ())

    def _go_alone(self, why: str) -> None:
        """Drop the play to its big picture alone, as a plain stream copy: the two pictures
        cannot be combined. The small picture's connection is given back, and the play says
        so. Runs on a build thread, under the play's `_swap_lock`."""
        session = self.session
        log.warning('Viewer %s: %s cannot be combined (%s), so it plays its big picture alone',
                    session.id, session.label, why)
        feed._stop_current(session)
        with feed._lock:
            session.combine_off = why
        feed._note_once(session, 'The two pictures could not be combined, so this play shows the big '
                                 f'picture alone and the small picture is off ({why[:200]}).')
        # Its pictures' connections go back first: the big picture alone needs one of them.
        leave(session)
        feed.play_alone(session)

    # -- the small picture's decoder ---------------------------------------------------------

    def _stop_decoder(self) -> None:
        with feed._lock:
            proc, self.decoder = self.decoder, None
            threads, self._decoder_threads = self._decoder_threads, []
            stop, self._small_stop = self._small_stop, None
            path, self.decoder_stderr = self.decoder_stderr, ''
        if stop is not None:
            stop.set()
        if proc is not None:
            terminate_or_kill(proc, hard=True)
        _join(threads)
        self.frames.clear()
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass   # already gone

    def _rebuild_decoder(self, want: tuple) -> None:
        if want != self.small_now:
            self.decoder_deaths = 0     # a new source is a new try
        self._stop_decoder()
        if self.closed:
            return
        session = self.session
        with feed._lock:
            shape, small, frames, geometry = self._shape, self.small, self.frames, self.geometry
            if want[0] == INPUT_NONE:
                self.small_now = want if self._shape == shape else None
        if want[0] == INPUT_NONE:
            return
        if want[0] != INPUT_LIVE:
            raise ValueError(f'Unknown small input "{want[0]}"')
        matrix, deinterlace = probe_picture(self.cfg, small.buffer.snapshot())
        cmd = build_decoder_cmd(self.cfg, self.device, geometry, gpu_decode=self.small_gpu,
                                deinterlace=deinterlace, matrix=matrix)
        reader = small.buffer.reader()
        try:
            fd, path = tempfile.mkstemp(prefix='channelbin-pip-small-', suffix='.stderr')
            os.close(fd)
            with open(path, 'wb') as err:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                        bufsize=0)
        except OSError as exc:
            log.error('Viewer %s: the small picture\'s decoder could not start: %s', session.id, exc)
            feed._note_once(session, f'The small picture could not be decoded ({exc}).')
            self.small_now = want
            self.decoder_deaths = DECODER_MAX_DEATHS
            return
        stop = threading.Event()
        threads = [threading.Thread(target=pump, args=(reader, proc.stdin, stop), daemon=True,
                                    name=f'pip-small-{session.id}'),
                   threading.Thread(target=frames.read_from, args=(proc.stdout,), daemon=True,
                                    name=f'pip-frames-{session.id}')]
        with feed._lock:
            dead = self.closed
            if not dead:
                self.decoder, self.decoder_stderr = proc, path
                self._decoder_threads, self._small_stop = threads, stop
                # None when the windows changed while this was building, as for the combiner.
                self.small_now = want if self._shape == shape else None
        if dead:
            terminate_or_kill(proc, hard=True)
            for pipe in (proc.stdin, proc.stdout):
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass   # gone with the process
            try:
                os.unlink(path)
            except OSError:
                pass   # already gone
            return
        for t in threads:
            t.start()

    def _decoder_died(self, proc) -> None:
        """The small picture has a channel and its decoder exited. Once on the CPU when its GPU
        decode is what failed, said on the play; else again, DECODER_MAX_DEATHS times in a row
        for one source, and then the small window says it cannot be shown."""
        from .preview import gpu_decode_failure
        session = self.session
        with feed._lock:
            if self.decoder is not proc:
                return
            path = self.decoder_stderr
        blamed = gpu_decode_failure(path) if self.small_gpu else ''
        tail = read_stderr_tail(path)
        self.decoder_died_at = time.monotonic()
        if blamed:
            self.small_gpu = False
            name = self.small.pane.name or 'the small picture'
            feed._note_once(session, f'The GPU could not decode {name}, so it is decoded on the CPU '
                                     f'for this play.')
            log.warning('Viewer %s: the GPU could not decode the small picture (%s), so its decoder '
                        'restarts on the CPU', session.id, blamed)
            self._rebuild_decoder(self.small_now)
            return
        self.decoder_deaths += 1
        log.warning('Viewer %s: the small picture\'s decoder exited with code %s, time %d%s', session.id,
                    proc.returncode, self.decoder_deaths, f': {tail}' if tail else '')
        if self.decoder_deaths >= DECODER_MAX_DEATHS:
            feed._note_once(session, 'The small picture could not be decoded, so its window stays on a '
                                     f'card until its channel changes{f" ({tail[:200]})" if tail else ""}.')
            self._stop_decoder()
            return
        self._rebuild_decoder(self.small_now)

    # -- the end -----------------------------------------------------------------------------

    def close(self) -> None:
        """Stop everything this play ran beside its session and let go of the pair. The
        caller has killed the combiner (output_feed._finish, or _stop_current). Idempotent."""
        with feed._lock:
            if self.closed:
                return
            self.closed = True
            threads, self._threads = self._threads, []
            stop, self._big_stop = self._big_stop, None
        if stop is not None:
            stop.set()
        self._stop_decoder()
        _join(threads)
        _release(self.pair, self.session)

    def set_geometry(self, geometry) -> None:
        """The row's layout was saved while this play watches it: the next combiner and decoder
        are built to it, at the reaper's next turn. The frames in hand are the old size, so the
        small window shows its card until the new decoder's first frame."""
        with feed._lock:
            if self.closed:
                return
            self._geometry_locked(geometry)

    def _geometry_locked(self, geometry) -> None:
        if geometry == self.geometry:
            return
        self.geometry = geometry
        self.frame_bytes = pip_frame_bytes(geometry)
        self.frames = NewestFrame(self.frame_bytes)
        self.big_now = self.small_now = None
        self._shape += 1

    def attach_locked(self) -> None:
        """Read this play's two pictures and layout off the pair again, as it attaches to its
        session: a Save during its tune-in swapped the pair's roles and stored the new layout,
        while this play was not yet one row_changed() lists. Nothing is built yet, so only the
        windows move. The caller holds output_feed._lock."""
        roles = (pip_channels.ROLE_INSET, pip_channels.ROLE_MAIN) if self.swapped else pip_channels.ROLES
        self.big, self.small = self.pair.pictures[roles[0]], self.pair.pictures[roles[1]]
        if self.pair.geometry is not None:
            self._geometry_locked(self.pair.geometry)

    def view(self) -> dict:
        return {'big': self.big.view(), 'small': self.small.view()}

    def title(self) -> str:
        return ' + '.join(p.pane.name or 'nothing' for p in (self.big, self.small))


# ── What output_feed calls ─────────────────────────────────────────────────────────────────

def begin(session, tuned, cfg: dict, device: str, wait: bool = True) -> bool:
    """Put `session`, a registered play of a set-up picture-in-picture channel, on its combined
    picture: take the row's pair, ask each picture that is not playing (the big one first),
    and leave the combiner to the reaper's next turn, once the big picture's first bytes are
    in. False, with nothing held, when neither picture can play right now: the caller shows
    the channel's card. Requires an app context."""
    from flask import current_app
    from . import db
    from .database import PipChannel
    row = db.session.get(PipChannel, tuned.row_id)
    if row is None:
        return False
    geometry = pip_geometry(row.layout, row.corner, row.size)
    pair = _acquire(tuned.row_id, row.name, tuned.swapped, session, current_app._get_current_object())
    combined = Combined(session, pair, tuned.swapped, geometry, device, cfg)
    try:
        # The channel-change grace once, for the big picture: a small picture refused a seat
        # asks again by itself the moment the play being left lets its connection go.
        combined.big.ask(cfg, wait=wait)
        combined.small.ask(cfg, wait=False)
    except BaseException:
        # The play never got onto the pair: without this it would be counted as on it for
        # good, and the pair would never linger out.
        combined.closed = True
        _release(pair, session)
        feed._ensure_reaper()
        raise
    with feed._lock:
        playing = combined.big.live or combined.small.live
        ok = playing and session.live and not session._finishing
        if ok:
            combined.attach_locked()
            session.combined = combined
    # Running either way: a pair nothing plays is the reaper's to close after its linger.
    feed._ensure_reaper()
    if not ok:
        combined.closed = True
        _release(pair, session)
        return False
    log.info('Viewer %s: %s plays its two pictures combined (%s big, %s small)', session.id,
             session.label, combined.big.pane.name, combined.small.pane.name)
    return True


def detach(session) -> dict:
    """A play is ending (output_feed._finish, its combiner already killed): stop its decoder,
    pacer and writers, let go of its pictures, and hand back the provider time they ran up,
    by account. No database."""
    combined = session.combined
    if combined is None:
        return {}
    seconds: dict = {}
    for picture in (combined.big, combined.small):
        for account_id, secs in picture.claim_seconds().items():
            seconds[account_id] = seconds.get(account_id, 0.0) + secs
    combined.close()
    return seconds


def leave(session) -> None:
    """Take a live play off its combined picture, for good: it goes to its card or to its big
    picture alone. Its decoder, pacer and writers stop, its pictures are let go of and, with no
    other play on them, closed now, so their connections are free for what it does next. The
    caller has stopped the combiner and holds the play's `_swap_lock`."""
    combined = session.combined
    if combined is None:
        return
    seconds = detach(session)
    with feed._lock:
        session.combined = None
        for account_id, secs in seconds.items():
            session.account_seconds[account_id] = session.account_seconds.get(account_id, 0.0) + secs
    _close_pair(combined.pair, feed.REASON_CLIENT_GONE)


def check(session, proc, now: float, state: str) -> None:
    """The reaper's turn for a combined play's own ffmpeg, the combiner. A stall of the big
    picture is its own session's to judge and fail over from; this sees a combiner that exited,
    never sent anything, or went silent while the big picture kept arriving (a hang:
    dev/docs/BUGS.md 2026-11-25 @ 08:02:19 PM), and restarts it as it does one that exited."""
    combined = session.combined
    rc = proc.poll()
    if rc is not None:
        combined.combiner_died(proc, f'the combining ffmpeg exited with code {rc}')
    elif state == feed.STATE_STARTING:
        if now - session.launched_mono > feed.CONNECT_TIMEOUT_SECONDS:
            combined.combiner_died(proc, 'the combining ffmpeg sent nothing in time')
    else:
        silent = combined.silent_for(now)
        if silent is not None and silent > session.stall_timeout:
            combined.combiner_died(proc, f'the combining ffmpeg sent nothing for {silent:.0f}s while its '
                                         f'big picture kept arriving', worked_until=session.last_data_mono)


def tick() -> None:
    """The reaper's turn for the engine itself: collect pictures whose session stopped, ask
    again the ones that are due, run each play's rebuilds, close pairs past their linger."""
    now = time.monotonic()
    with feed._lock:
        pairs = list(_pairs.values())
        combos = [s.combined for s in feed._sessions.values() if s.live and s.combined is not None]
    for pair in pairs:
        with feed._lock:
            lingered = (not pair.plays and pair.idle_since is not None
                        and now - pair.idle_since >= feed.SWITCH_GRACE_SECONDS)
        if lingered and _close_pair(pair, feed.REASON_CLIENT_GONE):
            continue
        for picture in pair.ordered():
            picture.ended()
            with feed._lock:
                due = (pair.plays and not picture.live and not picture._asking
                       and (picture.ask_due or now - picture.last_ask_mono >= feed.SCREEN_RECHECK_SECONDS))
                periodic = due and not picture.ask_due
            if due:
                _ask_later(picture, periodic)
    for combined in combos:
        try:
            combined.tick(now)
        except Exception:                               # noqa: BLE001 - one play, never the reaper
            log.exception('Viewer %s: picture-in-picture turn failed', combined.session.id)


def _ask_later(picture: Picture, periodic: bool) -> None:
    with feed._lock:
        picture.ask_due = False
        picture.last_ask_mono = time.monotonic()

    def run():
        from .config import load_config
        try:
            with picture.pair.app.app_context():
                if periodic:
                    # Every channel that failed is at least a turn old, and nothing else would
                    # ever bring the picture back (as a play on its card asks, _recheck_card).
                    picture.failed_ids.clear()
                picture.ask(load_config())
        except Exception:                               # noqa: BLE001 - one picture's ask, never the feed
            log.exception('Picture in picture: %s could not ask what it plays', picture.label())

    threading.Thread(target=run, daemon=True, name=f'pip-ask-{picture.pair.row_id}-{picture.role}').start()


def views(session) -> Optional[dict]:
    """Watching now's two pictures for a combined play, None for any other."""
    combined = session.combined
    return combined.view() if combined is not None else None
