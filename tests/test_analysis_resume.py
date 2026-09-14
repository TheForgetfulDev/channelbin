"""A finished post-capture analysis is never run a second time, and the health observation
it blends is counted exactly once.

Guards dev/docs/BUGS.md 2026-09-13 @ 04:05:48 PM ET. do_postprocess() had no record that its
analysis phase had completed, so scheduler.py's CONCATENATING/ANALYZING startup sweep re-ran
the whole thing on every service restart: a full-file ffprobe over the joined .ts, and then
apply_capture_quality_correction() blending that recording's capture-quality observation into
its channel AGAIN, gated on nothing. Recording 17 ran the phase three times and recording 19
twice, leaving two channels holding scores health_recompute.observation_ledger() could not
reproduce - the ledger emits exactly one SOURCE_CAPTURE_CORRECTION per recording.

No real ffmpeg or ffprobe: the probing helpers are patched, following
tests/test_analyzing_status.py. The capture-quality blend is deliberately NOT patched in the
counting tests - it is the thing under test.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.database import (Alert, Channel, Recording, RecordingEvent,  # noqa: E402
                          DIAGNOSTICS, POSTCAPTURE_ANALYSIS_STARTED,
                          POSTCAPTURE_ANALYSIS_SKIPPED)


def _pp_config(**recording_overrides):
    """A load_config() stand-in that walks do_postprocess from the analysis phase straight to
    its Complete step. Runtime load_config() reads the real config.yaml (CLAUDE.md, Testing),
    so the code under test is patched rather than fed make_test_app overrides."""
    cfg = {
        'recording': {
            'gather_health_data': True,
            'post_process': {'enabled': False},
            'move_on_complete': {'enabled': False},
            'post_script': {'enabled': False},
            'dvr_output_dir': '/nonexistent-test-dir',
            'serialize_concat': False,
        },
        'channel_testing': {},
        'ffmpeg': {'path': 'ffmpeg', 'concat_pre_output_timeout_seconds': 60,
                   'concat_stall_seconds': 60},
    }
    cfg['recording'].update(recording_overrides)
    return lambda *a, **kw: cfg


class _RanInline:
    """What inline_thread_for() hands back in place of a Thread it already ran."""

    def start(self):
        return None

    def join(self, timeout=None):
        return None


def inline_thread_for(fn):
    """A threading.Thread replacement that runs `fn` on the calling thread and leaves every
    other thread in the process alone.

    resume_in_progress_recordings() needs a live APScheduler, so replacing Thread wholesale
    would make the scheduler's own executor run inline too. This is narrowed to the one target
    whose ordering the test is about.
    """
    real = threading.Thread

    def _factory(target=None, args=(), kwargs=None, daemon=None, **rest):
        if target is fn:
            target(*args, **(kwargs or {}))
            return _RanInline()
        return real(target=target, args=args, kwargs=kwargs or {}, daemon=daemon, **rest)

    return _factory


class AnalysisIsRunOncePerRecordingTests(unittest.TestCase):
    """The gate itself: what a restart does to a row whose analysis already finished."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()
        self.ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(self.ts_path, 'wb') as fh:
            fh.write(b'x' * 64)

    def tearDown(self):
        self.t.cleanup()

    def _rec(self, status='ANALYZING', **kw):
        rec = seed.make_recording(status=status, channel_id=self.ch.id,
                                  output_path=self.ts_path, **kw)
        db.session.commit()
        return rec.id

    def _run(self, rid, **cfg_overrides):
        """do_postprocess with every probing helper patched. Returns the three spies."""
        from app.postprocessor import do_postprocess
        gather = mock.Mock(return_value=({}, None))
        scan = mock.Mock(return_value=(False, {}, 'clean'))
        near_empty = mock.Mock()
        with mock.patch('app.config.load_config', _pp_config(**cfg_overrides)), \
             mock.patch('app.postprocessor._gather_recording_health', gather), \
             mock.patch('app.postprocessor._scan_recording_timeline', scan), \
             mock.patch('app.postprocessor._detect_near_empty_segments', near_empty):
            do_postprocess(self.t.app, rid, self.ts_path)
        db.session.expire_all()
        return gather, scan, near_empty

    def test_a_completed_analysis_is_not_read_back_again(self):
        """The wasted work half: 11.7 minutes of full-file ffprobe over two recordings,
        burned while both were parked yielding resources to a live recording."""
        rid = self._rec(analysis_completed_at=datetime(2026, 9, 13, 18, 10, 0))

        gather, scan, near_empty = self._run(rid)

        self.assertFalse(gather.called, 'the joined file was probed again for health data')
        self.assertFalse(scan.called, 'the full-file timeline scan ran again')
        self.assertFalse(near_empty.called)

    def test_a_completed_analysis_says_on_the_timeline_that_it_was_skipped(self):
        """Principle 1: the phase not running is a fact about this recording, not silence."""
        rid = self._rec(analysis_completed_at=datetime(2026, 9, 13, 18, 10, 0))

        self._run(rid)

        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rid, event_type=POSTCAPTURE_ANALYSIS_SKIPPED).count(), 1)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rid, event_type=POSTCAPTURE_ANALYSIS_STARTED).count(), 0,
            'the event log claims an analysis began that never ran')

    def test_the_status_is_never_what_decides(self):
        """ANALYZING is what an unfinished phase AND a finished-but-parked one both read, so
        a gate on status cannot tell them apart - which is the state the bug fires from."""
        rid = self._rec(status='ANALYZING', analysis_completed_at=datetime(2026, 9, 13, 18, 10))
        gather, _scan, _ne = self._run(rid)
        self.assertFalse(gather.called)

        other = self._rec(status='ANALYZING')          # same status, no completion record
        gather2, _s2, _n2 = self._run(other)
        self.assertTrue(gather2.called,
                        'a genuinely unfinished analysis was skipped on its status alone')

    def test_an_unfinished_analysis_is_redone_and_says_so_out_loud(self):
        """CLAUDE.md: a resumed attempt names what is being redone - an operator must not
        have to infer that an upgrade or crash cost them a phase."""
        rid = self._rec()
        db.session.add(RecordingEvent(
            recording_id=rid, event_type=POSTCAPTURE_ANALYSIS_STARTED,
            timestamp=datetime(2026, 9, 13, 16, 44), detail='Checking the joined file'))
        db.session.commit()

        with self.assertLogs('app.postprocessor', level='WARNING') as logs:
            gather, _scan, _ne = self._run(rid)

        self.assertTrue(gather.called, 'an unfinished analysis was skipped')
        self.assertTrue(any('did not finish' in line for line in logs.output),
                        f'nothing warned that the phase was being redone: {logs.output}')
        newest = (RecordingEvent.query
                  .filter_by(recording_id=rid, event_type=POSTCAPTURE_ANALYSIS_STARTED)
                  .order_by(RecordingEvent.id.desc()).first())
        self.assertIn('again', newest.detail)

    def test_a_cancelled_row_is_not_resurrected_by_the_skip_path(self):
        """The skip path writes a status like any other, so it takes the same
        preserve_cancelled_status guard the phase it replaces does."""
        rid = self._rec(status='ABORTED',
                        analysis_completed_at=datetime(2026, 9, 13, 18, 10, 0))

        self._run(rid)

        self.assertEqual(db.session.get(Recording, rid).status, 'ABORTED')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rid, event_type=POSTCAPTURE_ANALYSIS_SKIPPED).count(), 0)


class TheHealthObservationIsBlendedOnceTests(unittest.TestCase):
    """The serious half: a duplicate blend cannot be undone arithmetically, so every extra
    one permanently detaches the stored score from its own ledger."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()
        self.ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(self.ts_path, 'wb') as fh:
            fh.write(b'x' * 64)

    def tearDown(self):
        self.t.cleanup()

    def _run(self, rid):
        """do_postprocess with the probes patched but the REAL capture-quality blend."""
        from app.postprocessor import do_postprocess
        with mock.patch('app.config.load_config', _pp_config()), \
             mock.patch('app.postprocessor._gather_recording_health',
                        return_value=({}, None)), \
             mock.patch('app.postprocessor._scan_recording_timeline',
                        return_value=(False, {}, 'clean')), \
             mock.patch('app.postprocessor._detect_near_empty_segments'):
            do_postprocess(self.t.app, rid, self.ts_path)
        db.session.expire_all()

    def test_three_restarts_blend_one_capture_correction(self):
        """Recording 17's exact history: the phase reached three times. The ledger emits one
        SOURCE_CAPTURE_CORRECTION per recording however many times the phase runs, so any
        extra blend is a sample the stored score counts and nothing can replay."""
        from app.health_recompute import (observation_ledger, SOURCE_CAPTURE_CORRECTION)
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        self._run(rid)
        self._run(rid)
        self._run(rid)

        cfg = _pp_config()()
        channel = db.session.get(Channel, self.ch.id)
        ledger = observation_ledger(channel.id, cfg)
        self.assertEqual(
            len([o for o in ledger if o.kind == SOURCE_CAPTURE_CORRECTION]), 1)
        self.assertEqual(channel.health_score_sample_count, len(ledger),
                         'the stored score counts samples the ledger cannot account for')

    def test_the_stored_score_stays_reproducible_from_the_ledger(self):
        """The invariant the duplicates broke, stated the way the app itself checks it:
        replaying every observation that still counts must reproduce the stored number."""
        from app.health_recompute import observation_ledger, replay
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        self._run(rid)
        self._run(rid)

        cfg = _pp_config()()
        channel = db.session.get(Channel, self.ch.id)
        score, count, _updated = replay(observation_ledger(channel.id, cfg), cfg)
        self.assertAlmostEqual(channel.health_score, score, places=6,
                               msg='the stored score cannot be reproduced from the ledger')
        self.assertEqual(channel.health_score_sample_count, count)

    def test_the_completion_stamp_is_not_written_without_the_blend(self):
        """Atomicity, from the side that can be observed: the stamp rides the blend's own
        commit, so a blend that never lands must not leave the phase looking finished. A
        stamp committed separately afterwards would pass this only by luck of ordering -
        committed BEFORE the blend it would strand the observation forever."""
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        from app.postprocessor import do_postprocess
        with mock.patch('app.config.load_config', _pp_config()), \
             mock.patch('app.postprocessor._gather_recording_health',
                        return_value=({}, None)), \
             mock.patch('app.postprocessor._scan_recording_timeline',
                        return_value=(False, {}, 'clean')), \
             mock.patch('app.postprocessor._detect_near_empty_segments'), \
             mock.patch('app.health_score.blend_health_score',
                        side_effect=RuntimeError('blend exploded')):
            with self.assertRaises(RuntimeError):
                do_postprocess(self.t.app, rid, self.ts_path)

        db.session.expire_all()
        row = db.session.get(Recording, rid)
        self.assertIsNone(row.analysis_completed_at,
                          'the phase was stamped complete though its observation never landed')
        self.assertIsNone(row.capture_quality_breakdown)

    def test_the_stamp_is_written_even_when_health_data_is_off(self):
        """gather_health_data off means no blend at all, which is exactly why the artifact
        it would have written cannot be the gate - the phase still has to record finishing."""
        from app.postprocessor import do_postprocess
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        with mock.patch('app.config.load_config', _pp_config(gather_health_data=False)), \
             mock.patch('app.postprocessor._gather_recording_health',
                        return_value=({}, None)):
            do_postprocess(self.t.app, rid, self.ts_path)

        db.session.expire_all()
        row = db.session.get(Recording, rid)
        self.assertIsNotNone(row.analysis_completed_at)
        self.assertIsNone(row.capture_quality_breakdown,
                          'a blend ran with gather_health_data off')


class RecordedTimelineScanTests(unittest.TestCase):
    """Skipping the phase must not simply move its ffprobe into the re-encode decision two
    hundred lines below, which is the only other caller of the scan."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Ch')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _scan_event(self, rid, extra, detail='Timeline scan: 2 gaps, 3.0s missing'):
        db.session.add(RecordingEvent(recording_id=rid, event_type=DIAGNOSTICS,
                                      detail=detail, extra_data=json.dumps(extra)))
        db.session.commit()

    def test_the_verdict_and_its_metrics_come_back_without_probing(self):
        from app.postprocessor import _recorded_timeline_scan
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  timeline_gap_count=2, timeline_gap_seconds=3.0,
                                  timeline_max_gap_seconds=2.0,
                                  timeline_deficit_seconds=3.0, timeline_damaged=True)
        db.session.commit()
        self._scan_event(rec.id, {'kind': 'timeline_scan', 'joined_segments': 2,
                                  'capture_fps_values': [25.0, 30.0], 'deficit_fps': 25.0})

        damaged, metrics, summary = _recorded_timeline_scan(rec.id)

        self.assertTrue(damaged)
        self.assertEqual(metrics['deficit_seconds'], 3.0)
        self.assertEqual(metrics['capture_fps_values'], [25.0, 30.0])
        self.assertEqual(summary, '2 gaps, 3.0s missing')

    def test_a_scan_that_failed_replays_as_a_failure_rather_than_a_re_probe(self):
        from app.postprocessor import _recorded_timeline_scan
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id)
        db.session.commit()
        self._scan_event(rec.id, {'kind': 'timeline_scan', 'scan_failed': True},
                         detail='Timeline scan: could not read the file')

        self.assertEqual(_recorded_timeline_scan(rec.id),
                         (False, {}, 'could not read the file'))

    def test_a_recording_that_never_scanned_returns_nothing_so_the_caller_scans(self):
        from app.postprocessor import _recorded_timeline_scan
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id)
        db.session.commit()
        self._scan_event(rec.id, {'kind': 'capture_health'})     # a different diagnostic

        self.assertIsNone(_recorded_timeline_scan(rec.id))


class MigrationBackfillTests(unittest.TestCase):
    """_m057 against a bare database of the vintage the step actually meets."""

    def _scratch(self, td):
        import sqlite3
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        cur.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, completed_at DATETIME, '
                    'capture_quality_breakdown TEXT)')
        cur.execute('CREATE TABLE recording_events (id INTEGER PRIMARY KEY, '
                    'recording_id INTEGER NOT NULL, timestamp DATETIME NOT NULL, '
                    'event_type VARCHAR(64) NOT NULL)')
        conn.commit()
        return conn, cur

    def test_a_finished_analysis_is_stamped_and_an_unfinished_one_is_not(self):
        """Leaving every pre-column row NULL is not the neutral choice: the two recordings
        still parked in ANALYZING would have re-analyzed and re-blended on the next restart,
        re-corrupting the scores this same migration's repair obligation exists to fix."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._scratch(td)
            cur.execute("INSERT INTO recordings (id, capture_quality_breakdown) "
                        "VALUES (1, '{\"final\": 90}'), (2, NULL)")
            cur.executemany(
                'INSERT INTO recording_events (recording_id, timestamp, event_type) '
                'VALUES (?, ?, ?)',
                [(1, '2026-09-13 16:44:12', 'POSTCAPTURE_ANALYSIS_STARTED'),
                 (1, '2026-09-13 18:10:00', 'POSTCAPTURE_ANALYSIS_STARTED'),
                 (2, '2026-09-13 18:10:00', 'POSTCAPTURE_ANALYSIS_STARTED')])
            conn.commit()

            M._m057_analysis_completion_stamp(conn, cur)

            stamped = dict(cur.execute(
                'SELECT id, analysis_completed_at FROM recordings').fetchall())
            self.assertEqual(stamped[1], '2026-09-13 18:10:00')
            self.assertIsNone(stamped[2],
                              'a recording whose blend never landed was stamped complete')
            conn.close()

    def test_the_repair_obligation_is_registered_and_left_pending(self):
        """The obligation is committed here, and discharged by ORM code create_app() runs -
        so a crash in between finds a pending obligation, never a silently skipped repair."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._scratch(td)
            M._m057_analysis_completion_stamp(conn, cur)

            row = cur.execute(
                'SELECT completed_at FROM migration_backfills WHERE name = ?',
                (M._BF_DUPLICATE_CAPTURE_CORRECTIONS,)).fetchone()
            self.assertIsNotNone(row, 'nothing recorded that the repair still owes work')
            self.assertIsNone(row[0])
            conn.close()

    def test_the_step_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._scratch(td)
            M._m057_analysis_completion_stamp(conn, cur)
            M._m057_analysis_completion_stamp(conn, cur)
            cols = [r[1] for r in cur.execute('PRAGMA table_info(recordings)')]
            self.assertEqual(cols.count('analysis_completed_at'), 1)
            conn.close()


class RepairDuplicatedCorrectionsTests(unittest.TestCase):
    """The scores already written. A blend is a lossy exponential average, so the only way
    back is a full replay of what still counts (CLAUDE.md canonical-homes table)."""

    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Cw 18')
        db.session.commit()
        self.cfg = _pp_config()()

    def tearDown(self):
        self.t.cleanup()

    def _corrupted_channel(self, analyses=3):
        """A channel whose stored score counted one recording's correction `analyses` times,
        exactly as a repeated post-capture analysis left it."""
        from app.health_score import blend_health_score
        base = datetime(2026, 9, 13, 16, 0, 0)
        # The recording carries BOTH of its observations, as a real one does: the primary
        # capture-phase score and the post-process correction. Two different qualities, so a
        # duplicated correction actually moves the number rather than re-blending it onto
        # itself.
        rec = seed.make_recording(status='ANALYZING', channel_id=self.ch.id,
                                  completed_at=base, health_quality_score=40,
                                  capture_quality_breakdown=json.dumps({'final': 80}))
        db.session.flush()
        for i in range(analyses):
            db.session.add(RecordingEvent(
                recording_id=rec.id, event_type=POSTCAPTURE_ANALYSIS_STARTED,
                timestamp=base + timedelta(minutes=30 * i), detail='Checking'))
        channel = db.session.get(Channel, self.ch.id)
        for quality, when in ([(40, base)]
                              + [(80, base + timedelta(minutes=30 * i))
                                 for i in range(analyses)]):
            score, count, updated, _b = blend_health_score(
                channel, quality, when, 1.0, self.cfg)
            channel.health_score = score
            channel.health_score_sample_count = count
            channel.health_score_updated_at = updated
        db.session.commit()
        return rec, channel

    def _arm_obligation(self):
        from sqlalchemy import text
        db.session.execute(text(M._BACKFILL_LEDGER_DDL))
        db.session.execute(
            text('INSERT OR REPLACE INTO migration_backfills (name, registered_at, '
                 'completed_at) VALUES (:n, :t, NULL)'),
            {'n': M._BF_DUPLICATE_CAPTURE_CORRECTIONS, 't': '2026-09-13 19:00:00'})
        db.session.commit()

    def test_the_repaired_score_is_the_one_the_ledger_replays_to(self):
        from app.health_recompute import (observation_ledger, replay,
                                          repair_duplicated_capture_corrections)
        _rec, channel = self._corrupted_channel(analyses=3)
        self._arm_obligation()
        before = channel.health_score
        expected, expected_count, _u = replay(observation_ledger(channel.id, self.cfg), self.cfg)

        repair_duplicated_capture_corrections(self.cfg)

        db.session.expire_all()
        channel = db.session.get(Channel, self.ch.id)
        self.assertAlmostEqual(channel.health_score, expected, places=6)
        self.assertEqual(channel.health_score_sample_count, expected_count)
        self.assertNotAlmostEqual(channel.health_score, before, places=6,
                                  msg='the corrupted score was left exactly as it was')

    def test_the_move_is_explained_on_the_channel_and_announced(self):
        """A score moving with nothing the user did behind it is the number nobody can
        explain - the founding thesis inverted, and the reason this repair is loud."""
        from app.database import ChannelEvent, CHANNEL_HEALTH_RECOMPUTED
        from app.health_recompute import repair_duplicated_capture_corrections
        self._corrupted_channel(analyses=2)
        self._arm_obligation()

        repair_duplicated_capture_corrections(self.cfg)

        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=self.ch.id, event_type=CHANNEL_HEALTH_RECOMPUTED).count(), 1)
        self.assertTrue(Alert.query.filter_by(alert_type='HEALTH_SCORES_REPAIRED').count(),
                        'the scores moved with nothing on any surface saying so')

    def test_a_recording_analyzed_once_is_left_alone(self):
        """Detection is two recorded facts, not a guess: without the duplicate analyses this
        channel's residual from pruned observations would be silently dropped."""
        from app.health_recompute import repair_duplicated_capture_corrections
        _rec, channel = self._corrupted_channel(analyses=1)
        self._arm_obligation()
        before = channel.health_score

        repaired = repair_duplicated_capture_corrections(self.cfg)

        db.session.expire_all()
        self.assertEqual(repaired, [])
        self.assertAlmostEqual(db.session.get(Channel, self.ch.id).health_score, before,
                               places=6)

    def test_it_runs_once_and_then_never_again(self):
        from app.health_recompute import repair_duplicated_capture_corrections
        self._corrupted_channel(analyses=3)
        self._arm_obligation()

        first = repair_duplicated_capture_corrections(self.cfg)
        second = repair_duplicated_capture_corrections(self.cfg)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], 'the repair ran again on a later startup')

    def test_nothing_runs_when_no_obligation_was_ever_registered(self):
        """A fresh database never crosses _m057, so it must not pay for this at all."""
        from app.health_recompute import repair_duplicated_capture_corrections
        self._corrupted_channel(analyses=3)
        self.assertEqual(repair_duplicated_capture_corrections(self.cfg), [])


class StartupResumesEachRowOnceTests(unittest.TestCase):
    """One restart must not leave two post-process chains on one recording.

    Guards dev/docs/BUGS.md 2026-09-13 @ 04:24:11 PM ET. Observed live on recording 17 while
    shipping the gate above: case 1c of resume_in_progress_recordings() relaunches
    do_postprocess() for a CONVERTING row, whose first act is to write ANALYZING - which is
    exactly what case 1d selects on, so the row reappears in 1d's query milliseconds later and
    gets a second chain. 1c's own is_conversion_active() guard cannot catch it: it is checked
    once at entry, and both chains then sit in the collision wait for as long as the recording
    they are yielding to lasts before each spawns ffmpeg on the same output file.
    """

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)
        self.ts_path = os.path.join(self.t._tmpdir, 'show.ts')
        with open(self.ts_path, 'wb') as fh:
            fh.write(b'x' * 64)

    def tearDown(self):
        self.t.cleanup()

    def test_a_converting_row_is_not_also_resumed_as_an_analyzing_one(self):
        from app.scheduler import resume_in_progress_recordings
        rec = seed.make_recording(status='CONVERTING', name='stranded conversion',
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        launched = []

        def _fake_postprocess(app_obj, recording_id, ts):
            launched.append(recording_id)
            # What the real chain does first, and the whole reason 1d finds this row.
            with app_obj.app_context():
                r = db.session.get(Recording, recording_id)
                r.status = 'ANALYZING'
                db.session.commit()

        # The interleaving forced rather than raced for: 1c's chain reaches its ANALYZING
        # write before 1d runs its query. That is one of the two orderings the real code can
        # take - it is the one that happened on the live app - and a test that leaves it to
        # thread timing passes against the unfixed code most of the time, which is worse than
        # no test. Only this one target runs inline - see inline_thread_for().
        with mock.patch('app.postprocessor.do_postprocess', _fake_postprocess), \
             mock.patch('threading.Thread', inline_thread_for(_fake_postprocess)), \
             mock.patch('app.concatenator.do_concatenation') as concat:
            resume_in_progress_recordings(self.t.app)

        self.assertEqual(launched, [rid], 'the same recording was resumed twice')
        self.assertFalse(concat.called,
                         'case 1d picked up the row case 1c had just relaunched')

    def test_a_genuinely_stranded_analyzing_row_is_still_picked_up(self):
        """Non-vacuity: the exclusion must be scoped to the rows 1c just launched."""
        from app.scheduler import resume_in_progress_recordings
        rec = seed.make_recording(status='ANALYZING', name='stranded analysis',
                                  output_path=self.ts_path)
        db.session.commit()
        rid = rec.id

        with mock.patch('app.concatenator.do_concatenation') as concat:
            resume_in_progress_recordings(self.t.app)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not concat.called:
                time.sleep(0.01)

        self.assertTrue(concat.called, 'an ANALYZING row at restart is stranded forever')
        self.assertEqual(concat.call_args.args[1], rid)


if __name__ == '__main__':
    unittest.main()
