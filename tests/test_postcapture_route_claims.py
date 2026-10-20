"""Tier 2 - three post-capture routes and the state they act on.

Three defects from the recording-pipeline correctness pass (dev/changelog/1180), fixed
together in dev/changelog/1242. Each guards its own dev/docs/BUGS.md entry, all dated
2026-10-20:

  * Cancel conversion between two attempts of a live chain was read as a stranded row -
    the route deleted the parts and marked ABORTED while the chain spawned the next attempt;
  * a manual Resume re-armed start_<id> at a time already passed, so every resume also
    fired a start that refused itself;
  * the Retry-conversion route checked for a live chain before its thread claimed one, so a
    double-click reset the attempt budget and flashed "started" for a refused run.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.postprocessor as ppmod  # noqa: E402
import app.scheduler as schedmod  # noqa: E402
from app import db  # noqa: E402
from app import concatenator  # noqa: E402
from app.database import Recording, REC_STATUS_CONVERTING, REC_STATUS_PAUSED  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


class _RouteCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        with concatenator._active_concats_lock:
            concatenator._active_concats.clear()
        self.t.cleanup()

    def _with_ts(self, status, **kw):
        rec = seed.make_recording(status=status, name='conv', **kw)
        ts = os.path.join(self.t._tmpdir, f'rec_{rec.id}.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        rec.output_path = ts
        db.session.commit()
        return rec.id, ts


# ── Cancel between attempts ───────────────────────────────────────────────────
class CancelBetweenAttemptsTests(_RouteCase):
    """BUGS.md 2026-10-20 - Cancel conversion between two attempts of a live chain is read
    as a stranded row.

    _cancel_conversion decided "no live conversion" from request_cancel_conversion() alone,
    which sets the cancel flag only when an ffmpeg is registered. Between attempts nothing
    is, though the chain still holds the concat claim.
    """

    def setUp(self):
        super().setUp()
        self.rid, self.ts = self._with_ts(REC_STATUS_CONVERTING, conversion_attempts=1)
        self.partial = os.path.splitext(self.ts)[0] + '.mp4'
        with open(self.partial, 'wb') as fh:
            fh.write(b'half an encode')
        # The chain owns this recording and is between attempts: no ffmpeg registered.
        self.assertIsNone(concatenator._claim_concat(self.rid))

    def test_the_cancel_is_handed_to_the_chain_not_applied_to_the_row(self):
        resp = self.t.client.post(f'/recordings/{self.rid}/cancel-convert')
        self.assertEqual(resp.status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).status, REC_STATUS_CONVERTING,
                         'the route marked a live chain\'s row as stranded')
        self.assertTrue(ppmod._consume_cancel(self.rid),
                        'the chain was never told to stop before its next attempt')

    def test_the_chain_s_files_are_left_for_its_own_cancelled_path(self):
        self.t.client.post(f'/recordings/{self.rid}/cancel-convert')
        self.assertTrue(os.path.exists(self.partial),
                        'the route deleted an attempt\'s output under a live chain')
        self.assertTrue(os.path.exists(self.ts))

    def test_a_row_nothing_owns_is_still_marked_here(self):
        concatenator._release_concat(self.rid)
        self.t.client.post(f'/recordings/{self.rid}/cancel-convert')
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).status, 'ABORTED')
        self.assertFalse(os.path.exists(self.partial))


# ── A manual Resume re-arms the stop job only ─────────────────────────────────
class ResumeReArmsTheStopJobOnlyTests(_RouteCase):
    """BUGS.md 2026-10-20 - a manual Resume re-arms start_<id> at a time that has already
    passed, so every resume also fires a start that refuses itself."""

    def setUp(self):
        super().setUp()
        rec = seed.make_recording(status=REC_STATUS_PAUSED, name='paused')
        db.session.commit()
        self.rid = rec.id

    def _resume(self, path):
        registered = []

        def _add_job(**kwargs):
            registered.append(kwargs['id'])

        with mock.patch('app.routes.recordings.resume_recording'), \
             mock.patch.object(schedmod, '_add_job', _add_job):
            resp = self.t.client.post(path)
        return resp, registered

    def test_the_form_route_registers_the_stop_job_and_nothing_else(self):
        resp, registered = self._resume(f'/recordings/{self.rid}/resume')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(registered, [f'stop_{self.rid}'])

    def test_the_json_route_registers_the_stop_job_and_nothing_else(self):
        resp, registered = self._resume(f'/recordings/{self.rid}/resume-json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(registered, [f'stop_{self.rid}'])


# ── Retry conversion claims in the request ────────────────────────────────────
class RetryConvertClaimsInTheRequestTests(_RouteCase):
    """BUGS.md 2026-10-20 - the Retry-conversion route checks for a live chain before its
    thread claims one, so a double-click can reset the attempt budget and flash "started"
    for a refused run.

    The worker thread is replaced by one that never starts, so the claim it used to take is
    never taken by it: the second request is refused only if the route itself claimed.
    """

    def setUp(self):
        super().setUp()
        self.rid, self.ts = self._with_ts('FAILED', conversion_attempts=3)
        self.threads = []

        case = self

        class _NeverRuns:
            def __init__(self, *a, **kw):
                case.threads.append(kw)

            def start(self):
                pass

        self._thread = mock.patch('app.routes.recordings.threading.Thread', _NeverRuns)
        self._thread.start()

    def tearDown(self):
        self._thread.stop()
        super().tearDown()

    def _retry(self):
        return self.t.client.post(f'/recordings/{self.rid}/retry-convert')

    def test_the_second_click_is_refused_and_writes_nothing(self):
        self.assertEqual(self._retry().status_code, 302)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        self.assertEqual(rec.status, 'ANALYZING')
        self.assertEqual(rec.conversion_attempts, 0)
        self.assertEqual(len(self.threads), 1)
        # The run in progress spends some of its budget...
        rec.conversion_attempts = 2
        db.session.commit()
        # ...and the second click must not hand it back.
        self.assertEqual(self._retry().status_code, 302)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rid).conversion_attempts, 2,
                         'the refused Retry reset the live run\'s attempt budget')
        self.assertEqual(len(self.threads), 1, 'the refused Retry started a second worker')

    def test_the_worker_inherits_the_claim(self):
        self._retry()
        self.assertTrue(self.threads[0]['kwargs'].get('claimed'))
        self.assertTrue(concatenator.is_concat_active(self.rid))

    def test_a_refused_start_releases_the_claim(self):
        os.unlink(self.ts)   # the route refuses after claiming: nothing to convert
        self.assertEqual(self._retry().status_code, 302)
        self.assertFalse(concatenator.is_concat_active(self.rid),
                         'a Retry that never started kept the chain claim')
        self.assertEqual(self.threads, [])


if __name__ == '__main__':
    unittest.main()
