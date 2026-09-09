"""Guards dev/docs/BUGS.md 2026-08-15 "Retry concat starts a second concatenation for a
recording that is merely queued".

CONCATENATING is not evidence that a concat thread is dead. A row sits there for as long as
`serialize_concat` makes its chain wait (`_wait_for_no_active_recording`, then `_concat_lock`),
and the very same status is what a crash mid-concat leaves behind - which is why the Retry
route accepts it. With no registry of live chains the route could not tell the two apart and
started a second `do_concatenation`: two ffmpegs writing one `output_path`, both deleting the
same segment files, and two `do_postprocess` chains behind them.

The route is not the only reachable duplicate, which is why the guard lives inside
`do_concatenation` itself: a scheduled stop job racing a manual Stop gives the second caller
`had_state=False` and a row still reading IN_PROGRESS, so `stop_recording()` launches a second
chain of its own (app/recorder.py), and the startup CONCATENATING sweep is a third launcher.

No real ffmpeg and no network: `do_postprocess` is patched, the only segment is a handful of
bytes written under the test's own temp dir, and the concurrency test blocks a stubbed
`_run_concatenation` on an Event rather than racing real work.
  python3 -m unittest tests.test_concat_double_run_guard
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

from app import db  # noqa: E402
from app import concatenator  # noqa: E402
from app.concatenator import do_concatenation, is_concat_active  # noqa: E402
from app.database import Recording, RecordingEvent  # noqa: E402


class _ConcatTestCase(unittest.TestCase):
    """A recording with one real segment file, in a temp dvr dir every runtime
    load_config() call sees."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {
            'dvr_output_dir': self.dvr,
            'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
            # persist_final_thumbnail spawns ffmpeg and nothing here asserts on thumbnails.
            'live_thumbnail': {'enabled': False},
            'post_process': {'enabled': False},
        }})

    def tearDown(self):
        concatenator._release_concat(self.rid) if getattr(self, 'rid', None) else None
        self.t.cleanup()

    def _recording_with_one_segment(self, status='IN_PROGRESS', name='Queued Concat'):
        rec = seed.make_recording(status=status, name=name)
        db.session.commit()
        self.rid = rec.id
        seg_path = os.path.join(self.dvr, f'{name}_{rec.id}_seg_001.ts')
        with open(seg_path, 'wb') as fh:
            fh.write(b'\x47' * 512)
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        from app.database import RecordingSegment
        db.session.add(RecordingSegment(
            recording_id=rec.id, segment_number=1, file_path=seg_path,
            started_at=now - timedelta(minutes=5), ended_at=now,
            exit_reason='STOP_TIME_REACHED', bytes_recorded=512))
        db.session.commit()
        self.seg_path = seg_path
        return rec.id


class ClaimGuardTests(_ConcatTestCase):
    def test_second_chain_for_a_live_id_does_no_work(self):
        """The corruption case: the queued chain owns the segments and the output path, so
        a second chain must not touch either."""
        rid = self._recording_with_one_segment()
        self.assertIsNone(concatenator._claim_concat(rid), 'setup: claim should be free')

        with mock.patch('app.postprocessor.do_postprocess') as postprocess:
            do_concatenation(self.t.app, rid)

        self.assertFalse(postprocess.called,
                         'a second chain reached post-processing for an already-claimed id')
        self.assertTrue(os.path.exists(self.seg_path),
                        'a second chain consumed the live chain\'s segment file')
        self.assertEqual([], [f for f in os.listdir(self.dvr) if f.endswith('.ts')
                              and f != os.path.basename(self.seg_path)],
                         'a second chain wrote a concat output while another chain was live')
        db.session.expire_all()
        self.assertEqual('IN_PROGRESS', db.session.get(Recording, rid).status)
        self.assertEqual([], RecordingEvent.query.filter_by(recording_id=rid).all(),
                         'a refused chain logged recording events as if it had run')

    def test_second_chain_never_enters_the_body(self):
        rid = self._recording_with_one_segment()
        concatenator._claim_concat(rid)

        with mock.patch.object(concatenator, '_run_concatenation') as body:
            do_concatenation(self.t.app, rid)

        self.assertFalse(body.called)

    def test_two_concurrent_launchers_produce_one_chain(self):
        """stop_recording() racing its own scheduled stop job: both callers see a row that
        no chain has marked CONCATENATING yet, so a check-then-set would let both through."""
        rid = self._recording_with_one_segment()
        entered = []
        hold = threading.Event()

        def _blocking_body(app, recording_id, *, reason):
            entered.append(recording_id)
            hold.wait(5)

        with mock.patch.object(concatenator, '_run_concatenation', _blocking_body):
            threads = [threading.Thread(target=do_concatenation, args=(self.t.app, rid),
                                        daemon=True) for _ in range(2)]
            for th in threads:
                th.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not entered:
                time.sleep(0.01)
            # Both threads have had their chance at the claim by now; the loser returns
            # immediately rather than blocking, so give it a moment to have done so.
            time.sleep(0.2)
            self.assertEqual(1, len(entered),
                             f'{len(entered)} chains ran concurrently for one recording')
            hold.set()
            for th in threads:
                th.join(5)

        self.assertFalse(is_concat_active(rid), 'the claim outlived the chain that held it')


class ClaimReleaseTests(_ConcatTestCase):
    def test_claim_is_released_after_a_completed_chain(self):
        """A held-forever claim would make the guard permanent: no later retry could ever
        rescue this recording."""
        rid = self._recording_with_one_segment()

        with mock.patch('app.postprocessor.do_postprocess'):
            do_concatenation(self.t.app, rid)

        self.assertFalse(is_concat_active(rid))

    def test_claim_is_released_when_the_chain_raises(self):
        rid = self._recording_with_one_segment()

        with mock.patch.object(concatenator, '_run_concatenation',
                               side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                do_concatenation(self.t.app, rid)

        self.assertFalse(is_concat_active(rid),
                         'a chain that raised left its recording claimed forever')


class RetryConcatRouteTests(_ConcatTestCase):
    def test_retry_is_refused_while_a_chain_is_live(self):
        rid = self._recording_with_one_segment(status='CONCATENATING')
        concatenator._claim_concat(rid)

        with mock.patch('app.concatenator.do_concatenation') as launch:
            resp = self.t.client.post(f'/recordings/{rid}/retry-concat',
                                      follow_redirects=True)

        self.assertEqual(200, resp.status_code)
        time.sleep(0.1)  # a spawned thread would have called by now
        self.assertFalse(launch.called,
                         'Retry concat launched a second chain for a live concatenation')
        self.assertIn('already running', resp.get_data(as_text=True).lower(),
                      'the refusal did not say why (Product Principle 1)')

    def test_retry_still_rescues_a_stranded_row(self):
        """Control - passes with or without the guard. It is here because the guard must
        not cost the rescue case the route exists for: no chain is claimed, so a row
        stranded in CONCATENATING by a crash still retries."""
        rid = self._recording_with_one_segment(status='CONCATENATING')
        self.assertFalse(is_concat_active(rid), 'setup: nothing should be claimed')

        with mock.patch('app.concatenator.do_concatenation') as launch:
            self.t.client.post(f'/recordings/{rid}/retry-concat', follow_redirects=True)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not launch.called:
                time.sleep(0.01)

        self.assertTrue(launch.called)


class StartupSweepTests(_ConcatTestCase):
    def setUp(self):
        super().setUp()
        self.t.cleanup()
        # resume_in_progress_recordings() sweeps orphaned on-demand jobs at the end, which
        # needs a live scheduler (same reason as tests/test_concat_startup_recovery.py).
        self.t = make_test_app(start_scheduler=True)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def test_sweep_skips_a_row_a_live_chain_already_owns(self):
        from app.scheduler import resume_in_progress_recordings

        rec = seed.make_recording(status='CONCATENATING', name='owned by a live chain')
        db.session.commit()
        self.rid = rec.id
        concatenator._claim_concat(self.rid)

        with mock.patch('app.concatenator.do_concatenation') as launch:
            resume_in_progress_recordings(self.t.app)
            time.sleep(0.2)

        self.assertFalse(launch.called,
                         'the startup sweep relaunched a concat another chain already owns')


if __name__ == '__main__':
    unittest.main()
