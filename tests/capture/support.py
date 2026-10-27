"""The capture tier's footing: a simulated provider, accounts synced from it the way a user's
are, a real scheduler, short watchdog timings, and the answer key (DESIGN-simulator.md §4,
§3.12; dev/changelog/1223).

`CaptureCase` serves `CAPTURE_WORLD` - two backends, one Xtream panel and one single-seat
login each, so a group can fail over across two accounts - syncs both through the real
`_do_sync`, and gives a scenario three things:

- `record()` schedules a recording through `app/scheduler.py::schedule_recording`, so the
  start and stop jobs are the real ones and `start_recording` is reached the way it is in
  prod;
- `finish()` waits for it to reach a terminal status, every segment file it wrote kept by
  hard link (`SegmentKeeper`), since a successful join deletes them;
- `answer()` and `claims()` put the answer key's reading of those files beside ChannelBin's
  own figures, and `assertAgrees()` compares the two kind for kind.

Timings are scaled down, never faked (§4): the watchdog's thresholds are config, written into
the test app's sandboxed config.yaml so the recorder's own runtime `load_config()` reads
them, with `ffmpeg.read_timeout_seconds` kept below `watchdog.stall_timeout_seconds`
(dev/changelog/958).
"""
import copy
import datetime as dt
import os
import re
import threading
import time
import unittest
from fractions import Fraction
from typing import NamedTuple

from app import db
from app.accounts import _do_sync
from app.database import (Channel, Recording, RecordingEvent, RecordingSegment,
                          XtreamAccount, REC_STATUS_ABORTED, REC_STATUS_COMPLETED,
                          REC_STATUS_FAILED, REC_STATUS_SCHEDULED)
from app.scheduler import schedule_recording
from sim import build_world, serve_for_test
from sim import answer_key as ak
from sim.content import PROFILES
from sim.lineup import KIND_LIVE

from ..support.app import _deep_merge, make_test_app
from ..support.sim_streams import library

TERMINAL = (REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED)

# The loop every channel serves, and the one a format change moves to. Not the suite's tiny
# test loops: the watchdog probes a segment's format only once it holds 2 MiB
# (watchdog._PROBE_MIN_BYTES), which the 451 kb/s tiny loop takes 37 s to reach, so a
# recording on it never gets a format pin and none of the format rules are exercised. The SD
# loop (1,848 kb/s) gets there in about 6 s, burst included, and its 128 kb/s audio fills a
# 256 KiB output block every 16 s, the cadence dev/changelog/1209 measured. Rendered once per
# machine, about 27 s and 64 s (DESIGN-simulator.md §3.9).
CHANNEL_LOOP = 'h264_576p25_aac'
OTHER_LOOP = 'h264_720p2997_aac'
PLACEHOLDER_LOOP = 'placeholder_1080p30'
CAPTURE_LOOPS = (CHANNEL_LOOP, OTHER_LOOP, PLACEHOLDER_LOOP)

# Two backends behind one panel each, a 4 s connect burst (the real ones run 13 to 29 s, §7;
# scaled down with everything else) and one seat a login, as on every real account this app
# has been measured against.
CAPTURE_WORLD = {
    'seed': 23,
    'backends': [
        {'name': 'cove', 'channels': 8, 'content': CHANNEL_LOOP, 'burst_seconds': 4.0,
         'radio_share': 0.0, 'no_id_share': 0.0},
        {'name': 'reed', 'channels': 8, 'content': CHANNEL_LOOP, 'burst_seconds': 4.0,
         'radio_share': 0.0, 'no_id_share': 0.0},
    ],
    'panels': [
        {'name': 'cove-tv', 'backend': 'cove', 'url_shape': {'bare': 1.0}},
        {'name': 'reed-tv', 'backend': 'reed', 'url_shape': {'bare': 1.0}},
    ],
    'logins': [
        {'backend': 'cove', 'username': 'coveuser', 'password': 'covepass1',
         'panels': ['cove-tv'], 'max_connections': 1},
        {'backend': 'reed', 'username': 'reeduser', 'password': 'reedpass1',
         'panels': ['reed-tv'], 'max_connections': 1},
    ],
}

# The watchdog at a scale a test can wait for. read_timeout 2 < stall_timeout 8, so a silent
# socket is still ffmpeg's to repair in-process before the watchdog's growth check fires.
# early_fail_min_bytes sits well under one 4 s burst of the SD loop (about 0.9 MB), so an
# ordinary segment that connected is never mistaken for a dead stream, and a refused one
# (no bytes at all) always is.
CAPTURE_CONFIG = {
    'watchdog': {
        'poll_interval_seconds': 1,
        'stall_timeout_seconds': 8,
        'restart_delay_seconds': 1,
        'max_consecutive_failures': 10,
        'early_fail_window_seconds': 30,
        'early_fail_min_bytes': 200_000,
        'early_fail_abort_count': 3,
        'early_fail_abort_window_seconds': 120,
    },
    'ffmpeg': {'read_timeout_seconds': 2},
    # Stream copy into the mp4: a re-encode of a minute of SD video takes this box longer than
    # the capture did, and the conversion is not what these scenarios are about. The smoke
    # set (tests/test_capture_smoke.py) keeps the default and converts for real.
    'recording': {'live_thumbnail': {'enabled': False},
                  'post_process': {'reencode_mode': 'never'}},
}

# Losses shorter than this are left out of the answer's `lost`: the B-frame or two a segment
# boundary cuts (DESIGN-simulator.md §3.12), which is not what any scenario is about.
MIN_GAP = 0.3
# The connect burst every backend in CAPTURE_WORLD replays, and the channel loop's keyframe
# interval: a connection opens at the keyframe at or before its burst, so it can reach back
# up to that much further (DESIGN-simulator.md §3.10).
BURST = CAPTURE_WORLD['backends'][0]['burst_seconds']
KEYFRAME_SECONDS = PROFILES[CHANNEL_LOOP].gop / float(Fraction(PROFILES[CHANNEL_LOOP].rate))

_SEGMENT = re.compile(r'_(\d+)_seg_(\d+)\.ts$')


class Chan(NamedTuple):
    """A channel as a scenario holds it. Plain values, not an ORM row: every read in this tier
    starts a fresh session (`CaptureCase.fresh`), which detaches any row held across it."""
    id: int
    stream_id: int
    stream_url: str
    account_id: int


class Grp(NamedTuple):
    id: int


def at(seconds: float) -> dt.datetime:
    """The simulator's clock `seconds` from now: it runs on real time in this tier."""
    return dt.datetime.utcnow() + dt.timedelta(seconds=seconds)


class SegmentKeeper(threading.Thread):
    """Hard-links every file that appears in the DVR folder into `keep`, the moment it
    appears, until stopped.

    A link shares the file, so it sees every byte ffmpeg writes after it was made, and it
    survives the join deleting its segments and the conversion deleting the joined .ts.
    Nothing ChannelBin reads is touched: its own names stay where it put them."""

    def __init__(self, src: str, keep: str):
        super().__init__(name='capture-segment-keeper', daemon=True)
        self.src, self.keep = src, keep
        os.makedirs(keep, exist_ok=True)
        self._halt = threading.Event()
        self.kept = {}          # name -> kept path

    def run(self):
        while not self._halt.is_set():
            self.sweep()
            self._halt.wait(0.05)
        self.sweep()

    def sweep(self):
        try:
            names = os.listdir(self.src)
        except FileNotFoundError:
            return
        for name in names:
            if name in self.kept:
                continue
            path = os.path.join(self.src, name)
            if not os.path.isfile(path):
                continue
            dest = os.path.join(self.keep, name)
            try:
                os.link(path, dest)
            except FileNotFoundError:
                continue        # gone between the listing and the link
            self.kept[name] = dest

    def stop(self):
        self._halt.set()
        self.join(timeout=5)

    def segments(self, recording_id: int) -> list:
        """The recording's kept segment files, in segment order."""
        found = []
        for name, path in list(self.kept.items()):
            m = _SEGMENT.search(name)
            if m and int(m.group(1)) == recording_id:
                found.append((int(m.group(2)), path))
        return [p for _, p in sorted(found)]

    def kept_copy(self, path: str) -> str | None:
        return self.kept.get(os.path.basename(path)) if path else None


class CaptureCase(unittest.TestCase):
    world_source = CAPTURE_WORLD
    loops = CAPTURE_LOOPS   # every loop the world serves, rendered and parsed before a test
    config = {}             # a scenario's own settings, merged over CAPTURE_CONFIG

    def setUp(self):
        self.lib = library(*self.loops)
        # Parsed once per process, before any capture opens: the simulator parses a loop the
        # first time a connection asks for it, and the SD loop takes long enough under a busy
        # box to outlast ffmpeg's 2 s read timeout at open - a simulator start-up cost that
        # no real provider has, read by ffmpeg as a provider that did not answer.
        for name in self.loops:
            self.lib.load(name)
        self.world = build_world(copy.deepcopy(self.world_source))
        # The simulator is started before the app so it is stopped after it: cleanups run
        # last-in first-out, and the app's cleanup is what kills any capture still running.
        # Stopped the other way round, a watchdog could relaunch ffmpeg at a simulator that
        # is no longer listening, which netguard refuses.
        self.sim = serve_for_test(self, self.world, content=self.lib)
        self.t = make_test_app(start_scheduler=True)
        self.addCleanup(self.t.cleanup)
        self.configure()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        self.keeper = SegmentKeeper(self.dvr, os.path.join(self.t._tmpdir, 'kept'))
        self.keeper.start()
        self.addCleanup(self.keeper.stop)
        self.accounts = {panel: self._add_and_sync(panel)
                         for panel in self.world.panels}

    def configure(self, extra=None):
        """Write CAPTURE_CONFIG, the class's `config` and `extra` into the app's sandboxed
        config.yaml, read by every runtime load_config()."""
        cfg = _deep_merge(copy.deepcopy(CAPTURE_CONFIG), copy.deepcopy(self.config))
        self.t.sandbox_config(_deep_merge(cfg, extra or {}))

    # --- the provider, as a user adds it ----------------------------------------------------

    def _add_and_sync(self, panel: str) -> int:
        view = self.sim.account(panel)

        def _add():
            account = XtreamAccount(name=panel, base_url=view.base_url,
                                    username=view.username, password=view.password,
                                    status='OK')
            db.session.add(account)
            db.session.commit()
            return account.id

        account_id = _add()
        _do_sync(account_id, threading.Event())
        db.session.remove()
        if not Channel.query.filter_by(account_id=account_id).count():
            raise AssertionError(f'syncing {panel} imported no channels')
        return account_id

    def channel(self, panel: str = 'cove-tv', n: int = 0) -> Chan:
        """The n-th live channel ChannelBin imported from `panel`, by stream id."""
        backend = self.world.panels[panel].backend
        live = sorted(c.stream_id for c in self.world.lineups[backend].channels
                      if c.kind == KIND_LIVE)
        sid = live[n]
        ch = Channel.query.filter_by(account_id=self.accounts[panel], stream_id=sid).one()
        return Chan(ch.id, ch.stream_id, ch.stream_url, ch.account_id)

    def label(self, channel_id: int) -> str:
        """A ChannelBin channel as the simulator's ledger names it: `backend:stream id`."""
        ch = db.session.get(Channel, channel_id)
        panel = db.session.get(XtreamAccount, ch.account_id).name
        return f'{self.world.panels[panel].backend}:{ch.stream_id}'

    def group(self, channels, name: str = 'capture group') -> Grp:
        """A group of `channels`, ranked in that order by health score, with Recording on
        for every member - built the way the Groups page builds one and promoted the way a
        user promotes it (`build_group_with_members`, then `set_participation`)."""
        from app.channel_groups import build_group_with_members, set_participation
        ids = [c.id for c in channels]

        def _build():
            grp = build_group_with_members(name, ids)
            for rank, cid in enumerate(ids):
                db.session.get(Channel, cid).health_score = 100 - 10 * rank
            for m in grp.memberships:
                set_participation(m, 'recording_enabled', True)
            db.session.commit()
            return grp.id

        return Grp(_build())

    def username(self, panel: str) -> str:
        return self.sim.account(panel).username

    # --- recording ------------------------------------------------------------------------

    def record(self, channel: Chan | None = None, group: Grp | None = None,
               seconds: float = 20, start_in: float = 1.0, name: str = 'capture') -> int:
        """A recording scheduled the way the app schedules one: its row, then the scheduler's
        start and stop jobs. Returns its id."""
        start = dt.datetime.utcnow().replace(microsecond=0) + dt.timedelta(seconds=start_in)
        stop = start + dt.timedelta(seconds=seconds)

        def _add():
            rec = Recording(name=name, url=channel.stream_url if channel else '',
                            start_time=start, stop_time=stop, scheduled_start_time=start,
                            scheduled_stop_time=stop, status=REC_STATUS_SCHEDULED,
                            channel_id=channel.id if channel else None,
                            group_id=group.id if group else None)
            db.session.add(rec)
            db.session.commit()
            return rec.id

        rid = _add()
        schedule_recording(self.t.app, rid, start, stop)
        return rid

    def fresh(self, model, ident):
        """A row read past this thread's identity map: the capture threads commit in their
        own sessions (dev/changelog/1205)."""
        db.session.remove()
        return db.session.get(model, ident)

    def status(self, rid: int) -> str:
        return self.fresh(Recording, rid).status

    def wait_for(self, predicate, timeout: float, what, poll: float = 0.25):
        """Poll `predicate` until it returns something true. `what` is a string, or a
        callable for a description that has to be read when the wait gives up."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = predicate()
            if got:
                return got
            time.sleep(poll)
        got = predicate()
        if not got:
            raise AssertionError(f'gave up after {timeout:g} s waiting for '
                                 f'{what() if callable(what) else what}')
        return got

    def wait_status(self, rid: int, statuses, timeout: float) -> Recording:
        statuses = (statuses,) if isinstance(statuses, str) else tuple(statuses)
        self.wait_for(lambda: self.status(rid) in statuses, timeout,
                      lambda: f'recording {rid} to reach {"/".join(statuses)} (it is '
                              f'{self.status(rid)}; events: '
                              f'{[e.event_type for e in self.events(rid)]})')
        return self.fresh(Recording, rid)

    def finish(self, rid: int, timeout: float) -> Recording:
        """Wait for a terminal status, then stop keeping files."""
        rec = self.wait_status(rid, TERMINAL, timeout)
        self.keeper.sweep()
        return rec

    def events(self, rid: int, event_type: str | None = None) -> list:
        db.session.remove()
        q = RecordingEvent.query.filter_by(recording_id=rid)
        if event_type is not None:
            q = q.filter_by(event_type=event_type)
        return q.order_by(RecordingEvent.id).all()

    def event_types(self, rid: int) -> list:
        return [e.event_type for e in self.events(rid)]

    def assertEvent(self, rid: int, event_type: str) -> RecordingEvent:
        """The recording's first event of `event_type`, which must exist."""
        found = self.events(rid, event_type)
        self.assertTrue(found, f'recording {rid} has no {event_type}: {self.event_types(rid)}')
        return found[0]

    def wait_event(self, rid: int, event_type: str, timeout: float) -> RecordingEvent:
        return self.wait_for(lambda: next(iter(self.events(rid, event_type)), None), timeout,
                             lambda: f'{event_type} on recording {rid} (it has '
                                     f'{self.event_types(rid)})')

    def segments(self, rid: int) -> list:
        db.session.remove()
        return (RecordingSegment.query.filter_by(recording_id=rid)
                .order_by(RecordingSegment.segment_number).all())

    def wait_capturing(self, rid: int, timeout: float, min_bytes: int = 2_000_000):
        """Wait until the recording's newest segment file holds `min_bytes` - past the
        watchdog's format probe (2 MiB), so the capture is well under way."""
        def _big_enough():
            segs = self.segments(rid)
            if not segs:
                return False
            try:
                return os.path.getsize(segs[-1].file_path) >= min_bytes
            except OSError:
                return False
        return self.wait_for(_big_enough, timeout, f'recording {rid} to be capturing')

    def wait_segment(self, rid: int, number: int, timeout: float) -> RecordingSegment:
        def _found():
            return next((s for s in self.segments(rid) if s.segment_number == number), None)
        return self.wait_for(_found, timeout, f'segment {number} of recording {rid}')

    # --- the answer key against ChannelBin ----------------------------------------------------

    def answer(self, rid: int, **kw) -> ak.Answer:
        """The answer key's reading of every segment file the capture wrote, discarded ones
        included: what was captured, before ChannelBin chose what to join. Judged over the
        recording's scheduled window unless `window` is given: without one the answer key's
        extent runs from the first frame held to the last, and content lost before the first
        connection or after the last segment stopped is never counted (dev/changelog/1235)."""
        paths = []
        for seg in self.segments(rid):
            kept = self.keeper.kept_copy(seg.file_path)
            if kept is None:
                # ffmpeg never created the file: a capture that failed at open (refused,
                # timed out) writes nothing at all. An empty stand-in keeps the answer's
                # segments lined up with ChannelBin's rows, and the answer key reads it as a
                # segment holding nothing.
                kept = os.path.join(self.keeper.keep, os.path.basename(seg.file_path))
                open(kept, 'wb').close()
            paths.append(kept)
        kw.setdefault('serving', self.serving(rid))
        kw.setdefault('min_gap', MIN_GAP)
        kw.setdefault('window', self.window(rid))
        return ak.check(self.sim, paths, **kw)

    def answer_joined(self, rid: int, **kw) -> ak.Answer:
        """The answer key's reading of the joined file - what the user was handed - as one
        segment."""
        rec = self.fresh(Recording, rid)
        joined = [p for name, p in list(self.keeper.kept.items())
                  if name.endswith('.ts') and not _SEGMENT.search(name)
                  and name.startswith(os.path.splitext(os.path.basename(
                      rec.output_path or ''))[0] or '\0')]
        if len(joined) != 1:
            raise AssertionError(f'expected one joined .ts kept for recording {rid}, found '
                                 f'{joined} (output_path {rec.output_path})')
        kw.setdefault('serving', self.serving(rid))
        kw.setdefault('min_gap', MIN_GAP)
        kw.setdefault('window', self.window(rid))
        return ak.check(self.sim, joined, **kw)

    def claims(self, rid: int) -> dict:
        """What ChannelBin says about the recording, in the answer key's terms - only the
        figures that measure the same thing (§3.12's pairing table):

        - each kept segment's channel, from `RecordingSegment.channel_id`, and no channel
          for one ChannelBin recorded no bytes for (`bytes_recorded`);
        - each segment's placeholder content: a discarded segment's
          `content_duration_seconds` (which the watchdog sets to the clip's length when it
          discards it), and none for a kept one;
        - the recording's placeholder content, `discarded_seconds` (the rollup of those,
          `concatenator._record_discarded_rollup`).

        Deliberately not `content_duration_seconds` against `file_video_seconds` for a kept
        segment: ChannelBin's is ffprobe's container duration, which runs on through audio
        the video does not have, so a segment whose video stopped reads 24 s against 14 s
        of picture. Two different measures, not a disagreement. Figures that only relate
        within a bound (a wall clock against content, a file timeline against content) are
        each scenario's to check, with the bound explained there.
        """
        rec = self.fresh(Recording, rid)
        segs = self.segments(rid)
        out = {'segment_count': len(segs), 'segments': []}
        for s in segs:
            if not s.bytes_recorded:
                # ChannelBin recorded no bytes for it: an attempt that held nothing, whatever
                # member it was aimed at.
                out['segments'].append({'placeholder_seconds': 0.0, 'channels': ()})
            elif s.excluded:
                out['segments'].append({'placeholder_seconds': s.content_duration_seconds,
                                        'channels': ()})
            else:
                out['segments'].append({'placeholder_seconds': 0.0,
                                        'channels': (self.label(s.channel_id),)})
        if rec.discarded_seconds is not None:
            out['placeholder_seconds'] = rec.discarded_seconds
        return out

    def window(self, rid: int) -> tuple:
        """The stretch of the simulator clock the recording was scheduled to cover. The
        simulator runs on real time here, so it is the recording's own start and stop."""
        rec = self.fresh(Recording, rid)
        return (rec.scheduled_start_time, rec.scheduled_stop_time)

    def serving(self, rid: int) -> list:
        """The channels a recording could be served from, as the ledger names them: its group's
        recording members, or its own channel. Read from what the test set up, never from
        which member ChannelBin says it used, so the answer key's placement cannot be steered
        by the figure it is about to check."""
        rec = self.fresh(Recording, rid)
        if rec.group_id is not None:
            return [self.label(m.channel_id) for m in rec.group.memberships
                    if m.recording_enabled]
        return [self.label(rec.channel_id)]

    @staticmethod
    def lost(answer: ak.Answer, *reasons) -> list:
        return [x for x in answer.lost if x.reason in reasons]

    def assertGapsInsideDowntime(self, rid: int, slack: float = 1.0):
        """Capture gaps sit inside Downtime, but for time spent paused and the seconds before
        the first segment opened - what the recording page's Downtime tooltip and the
        `capture_gap_seconds` docstring promise. Nothing here pauses, so only the second
        exception is allowed for. `slack` covers the commit-to-commit slivers between one
        charge ending and the next beginning, measured under 1 s (dev/changelog/1303)."""
        # Read off the row before segments() opens a fresh session and detaches it.
        rec = self.fresh(Recording, rid)
        gap = rec.capture_gap_seconds or 0.0
        downtime = rec.total_downtime_seconds or 0.0
        start = rec.start_time
        segs = self.segments(rid)
        first = min((s.started_at for s in segs if s.started_at), default=None)
        before_first = (max(0.0, (first - start).total_seconds())
                        if first is not None else 0.0)
        self.assertLessEqual(
            gap, downtime + before_first + slack,
            f'Capture gaps {gap:.2f}s exceed Downtime {downtime:.2f}s by more than the '
            f'{before_first:.2f}s before the first segment opened (plus {slack:.1f}s slack); '
            f'segments: ' + ', '.join(
                f'#{s.segment_number} {s.bytes_recorded or 0}B '
                f'{(s.ended_at - s.started_at).total_seconds() if s.ended_at else "open"}s'
                for s in segs))

    def assertAgrees(self, answer: ak.Answer, claims: dict, tolerance: float = 0.5):
        wrong = answer.compare(claims, tolerance)
        self.assertEqual([str(m) for m in wrong], [],
                         f'ChannelBin and the answer key disagree\n{answer.describe()}')

    def assertNoProblems(self, answer: ak.Answer):
        self.assertEqual(answer.problems, [], answer.describe())
