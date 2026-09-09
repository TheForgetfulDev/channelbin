"""Tier 2 - restart.sh's busy guard + retry of a stranded conversion.

Guards dev/docs/BUGS.md 2026-07-23 09:xx PM ("restart.sh had no busy guard; a killed
conversion was unrecoverable") and dev/docs/BUGS.md's 2026-08-04 search-index restart
entry (a killed rebuild stranded the index at BUILDING). Invariants:

  (a) tools/check_busy.py exits non-zero and names the row for every status in
      RESTART_BLOCKING_STATUSES, and exits zero for COMPLETED/FAILED/ABORTED/SCHEDULED
      and for a missing DB file.
  (b) POST /recordings/<id>/retry-convert is accepted for a CONVERTING row whose .ts
      source exists on disk, and the detail page offers the action.
  (c) Both restart surfaces - tools/check_busy.py (the CLI/agent path) and
      POST /api/settings/restart (the in-app button) - also refuse while any search index
      is STATUS_BUILDING, and both let force/--force through anyway (dev/changelog/461).
  (d) Both surfaces refuse for the three blockers added in dev/changelog/732 - a health
      check run, a single-channel test, and an account mid-sync - and name each one. A
      health check spawns a real ffmpeg probe per channel for hours and was previously
      invisible to the guard entirely; the sync was reported but never blocked.
  (e) A health check run is counted once, not twice: the per-channel tests underneath it
      carry its job_id and must not also report themselves.
  (f) Nothing the guard blocks on can wedge restarts forever - a channel_tests row left
      open by a hard kill is closed at the next startup (dev/docs/BUGS.md 2026-08-18).

check_busy.py is invoked as a subprocess against the test app's temp DB - never the real
dvr.db - because that is exactly how restart.sh calls it.
"""
import os
import subprocess
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import (M3uAccount, Recording, RESTART_BLOCKING_STATUSES,  # noqa: E402
                          SearchIndexState, ChannelTest, TEST_STATUS_CANCELLED)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHECK_BUSY = os.path.join(_REPO_ROOT, 'tools', 'check_busy.py')


def run_check_busy(db_path):
    return subprocess.run([sys.executable, _CHECK_BUSY, '--db', db_path],
                          capture_output=True, text=True, cwd=_REPO_ROOT, timeout=120)


class CheckBusyTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_blocking_statuses_exit_nonzero_and_name_the_row(self):
        for status in RESTART_BLOCKING_STATUSES:
            rec = seed.make_recording(status=status, name=f'busy_{status}')
            db.session.commit()
            try:
                with self.subTest(status=status):
                    proc = run_check_busy(self.t.db_path)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn(f'#{rec.id}', proc.stdout)
                    self.assertIn(status, proc.stdout)
                    self.assertIn(f'busy_{status}', proc.stdout)
            finally:
                # Outside the subTest: a failed assertion must not leave the row behind
                # to contaminate the next status.
                db.session.delete(rec)
                db.session.commit()

    def test_idle_statuses_exit_zero(self):
        for status in ('SCHEDULED', 'COMPLETED', 'FAILED', 'ABORTED'):
            seed.make_recording(status=status)
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_empty_db_exits_zero(self):
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_missing_db_exits_zero(self):
        # A fresh install (no dvr.db yet) must still be startable.
        proc = run_check_busy(os.path.join(self.t._tmpdir, 'does-not-exist.db'))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_never_writes_to_the_database(self):
        seed.make_recording(status='CONVERTING')
        db.session.commit()
        before = os.stat(self.t.db_path).st_mtime_ns
        run_check_busy(self.t.db_path)
        self.assertEqual(os.stat(self.t.db_path).st_mtime_ns, before)

    def test_a_rebuilding_index_exits_nonzero_and_is_named(self):
        """The actual root cause of dev/changelog/461: killing a rebuild mid-transaction
        strands it at BUILDING with nobody left to finish it."""
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING))
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(SI.SEARCH_INDEX_PROGRAMS, proc.stdout)

    def test_an_ok_index_does_not_block(self):
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_OK))
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_syncing_account_blocks_and_is_named(self):
        """Advisory-only when it was added (dev/changelog/680), blocking since
        dev/changelog/732: "safely cancellable" is an argument about corruption, not about
        waste, and a restart mid-sync still costs the provider fetch plus up to ~25 minutes
        of degraded search before the index janitor repairs it."""
        db.session.add(M3uAccount(name='Provider Two', status='SYNCING',
                                  m3u_url='http://provider.test/playlist.m3u8'))
        db.session.add(M3uAccount(name='Provider Idle', status='OK',
                                  m3u_url='http://provider.test/other.m3u8'))
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('Provider Two', proc.stdout)
        self.assertIn('syncing', proc.stdout)
        # Both accounts in one run on purpose: this asserts the line discriminates on status
        # rather than merely printing accounts, and each run here costs a subprocess.
        self.assertNotIn('Provider Idle', proc.stdout)

    def test_a_syncing_account_is_still_named_alongside_a_blocking_recording(self):
        """Each blocker is named on its own line rather than the first one short-circuiting
        the rest - the operator deciding whether to --force needs the whole list."""
        rec = seed.make_recording(status='IN_PROGRESS', name='busy')
        db.session.add(M3uAccount(name='Provider Two', status='SYNCING',
                                  m3u_url='http://provider.test/playlist.m3u8'))
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f'#{rec.id}', proc.stdout)
        self.assertIn('Provider Two', proc.stdout)

    def test_a_running_health_check_blocks_and_is_named(self):
        """The gap this closes: a run spawns a real ffmpeg probe per channel and can run
        for hours, and the guard could not see it at all - restarting killed the live probe
        and abandoned the rest of the run with no warning."""
        job = seed.make_test_job(name='Nightly guide check', status='RUNNING')
        seed.make_test_job(name='Idle job', status='QUEUED')
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f'health check #{job.id}', proc.stdout)
        self.assertIn('Nightly guide check', proc.stdout)
        self.assertNotIn('Idle job', proc.stdout)

    def test_an_in_flight_single_channel_test_blocks_and_is_named(self):
        """A pre-record check or a "Test now" click has no job row, so the health check
        query above cannot see it - it is the open channel_tests row that reports it."""
        acc = seed.make_account()
        ch = seed.make_channel(acc, stream_id=1, name='Channel Under Test')
        # test_ended_at=None IS the in-flight state - the seeder stamps a finished row
        # by default, because a row with no end time is the one being tested right now.
        test = seed.make_channel_test(ch, test_ended_at=None)
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f'channel test #{test.id}', proc.stdout)
        self.assertIn('Channel Under Test', proc.stdout)

    def test_a_finished_channel_test_does_not_block(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, stream_id=1, name='Channel Under Test')
        seed.make_channel_test(ch, status='COMPLETED', test_ended_at=datetime.utcnow())
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_health_check_run_is_not_counted_twice_by_its_own_channel_test(self):
        """Every test a run performs carries that run's job_id. Reporting both would name
        the same work twice and make the blocking list read as two separate things."""
        acc = seed.make_account()
        ch = seed.make_channel(acc, stream_id=1, name='Channel Under Test')
        job = seed.make_test_job(name='Nightly guide check', channels=[ch], status='RUNNING')
        db.session.flush()
        seed.make_channel_test(ch, job_id=job.id, test_ended_at=None)
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn('health check', proc.stdout)
        self.assertNotIn('channel test #', proc.stdout)

    def test_blocking_kinds_names_every_kind_present_and_nothing_else(self):
        """restart.sh parses this line to decide whether its recordings-only "no ffmpeg
        under the DVR output dir, so this row may be stale" hint applies - a health check's
        probe writes to the system temp dir, so printing it for one would be wrong."""
        seed.make_test_job(name='Nightly guide check', status='RUNNING')
        db.session.add(M3uAccount(name='Provider Two', status='SYNCING',
                                  m3u_url='http://provider.test/playlist.m3u8'))
        db.session.commit()
        proc = run_check_busy(self.t.db_path)
        kinds = [ln for ln in proc.stdout.splitlines() if ln.startswith('blocking-kinds:')]
        self.assertEqual(len(kinds), 1, proc.stdout)
        self.assertIn('health-checks', kinds[0])
        self.assertIn('account-syncs', kinds[0])
        self.assertNotIn('recordings', kinds[0])

    def test_no_blocking_kinds_line_when_idle(self):
        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn('blocking-kinds:', proc.stdout)


class RetryConvertStrandedTests(unittest.TestCase):
    """A CONVERTING row left behind by a restart/crash must be restartable from the UI."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        # Address the .ts via the row's own output_path under the test tmpdir - never a
        # runtime load_config() dir, which points at the real /dvr.
        self.ts_path = os.path.join(self.t._tmpdir, 'stranded.ts')
        with open(self.ts_path, 'wb') as f:
            f.write(b'\x47' * 1024)
        self.rec = seed.make_recording(status='CONVERTING', name='stranded',
                                       output_path=self.ts_path)
        db.session.commit()
        self.rid = self.rec.id

    def tearDown(self):
        self.t.cleanup()

    def test_retry_convert_accepted_for_converting_row(self):
        with patch('app.postprocessor.do_postprocess') as fake:
            resp = self.t.client.post(f'/recordings/{self.rid}/retry-convert')
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f'/recordings/{self.rid}', resp.headers['Location'])
        # Accepted means the conversion was actually relaunched, not just redirected.
        for _ in range(100):
            if fake.called:
                break
            import time
            time.sleep(0.01)
        self.assertTrue(fake.called)
        db.session.expire_all()
        rec = db.session.get(Recording, self.rid)
        # ANALYZING, not CONVERTING: do_postprocess re-reads the whole .ts before it spawns
        # any ffmpeg, so claiming CONVERTING here would name a phase the app is not in yet
        # (dev/changelog/867).
        self.assertEqual(rec.status, 'ANALYZING')
        self.assertEqual(rec.conversion_attempts, 0)

    def test_retry_convert_refused_when_source_missing(self):
        os.unlink(self.ts_path)
        with patch('app.postprocessor.do_postprocess') as fake:
            resp = self.t.client.post(f'/recordings/{self.rid}/retry-convert',
                                      follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(fake.called)

    def test_detail_page_offers_retry_conversion(self):
        html = self.t.client.get(f'/recordings/{self.rid}').get_data(as_text=True)
        self.assertIn('data-act="retry-convert"', html)


class RestartApiBusyTests(unittest.TestCase):
    """POST /api/settings/restart must refuse while work is in flight, name the rows, and
    honour force:true - and it must never spawn restart.sh without --force, since the
    script's own guard would refuse invisibly (Popen output goes to DEVNULL)."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def post(self, body=None):
        # Popen is always patched: a real spawn would restart the live app from a test.
        with patch('app.routes.settings.subprocess.Popen') as popen:
            resp = self.t.client.post('/api/settings/restart',
                                      json=body if body is not None else {})
        return resp, popen

    def test_refuses_and_names_every_blocking_status(self):
        for status in RESTART_BLOCKING_STATUSES:
            rec = seed.make_recording(status=status, name=f'busy_{status}')
            db.session.commit()
            try:
                with self.subTest(status=status):
                    resp, popen = self.post()
                    self.assertEqual(resp.status_code, 409)
                    body = resp.get_json()
                    self.assertIn('error', body)
                    self.assertEqual([r['id'] for r in body.get('blocking', [])], [rec.id])
                    self.assertEqual(body['blocking'][0]['status'], status)
                    self.assertIn(f'busy_{status}', body['blocking'][0]['label'])
                    popen.assert_not_called()
            finally:
                # Outside the subTest: a failed assertion must not leave the row behind
                # to contaminate the next status.
                db.session.delete(rec)
                db.session.commit()

    def test_force_restarts_despite_in_flight_work(self):
        seed.make_recording(status='CONVERTING', name='converting one')
        db.session.commit()
        resp, popen = self.post({'force': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        popen.assert_called_once()
        self.assertIn('--force', popen.call_args[0][0])

    def test_a_rebuilding_index_is_refused_and_named(self):
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING))
        db.session.commit()
        resp, popen = self.post()
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        blocking = body.get('blocking', [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['status'], 'BUILDING')
        self.assertIn(SI.SEARCH_INDEX_PROGRAMS, blocking[0]['label'])
        popen.assert_not_called()

    def test_force_restarts_despite_a_rebuild(self):
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS, status=SI.STATUS_BUILDING))
        db.session.commit()
        resp, popen = self.post({'force': True})
        self.assertEqual(resp.status_code, 200)
        popen.assert_called_once()

    def test_a_running_health_check_is_refused_and_named(self):
        """The in-app button and the CLI block on the same set, so a health check has to
        reach this surface too - it is the one most users will meet (dev/changelog/732)."""
        seed.make_test_job(name='Nightly guide check', status='RUNNING')
        db.session.commit()
        resp, popen = self.post()
        self.assertEqual(resp.status_code, 409)
        blocking = resp.get_json().get('blocking', [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['status'], 'RUNNING')
        self.assertIn('Nightly guide check', blocking[0]['label'])
        # 'id' means "recording id" to the modal, so a row from any other table sends None.
        self.assertIsNone(blocking[0]['id'])
        popen.assert_not_called()

    def test_an_in_flight_channel_test_is_refused_and_named(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, stream_id=1, name='Channel Under Test')
        seed.make_channel_test(ch, test_ended_at=None)
        db.session.commit()
        resp, popen = self.post()
        self.assertEqual(resp.status_code, 409)
        blocking = resp.get_json().get('blocking', [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['status'], 'TESTING')
        self.assertIn('Channel Under Test', blocking[0]['label'])
        popen.assert_not_called()

    def test_a_syncing_account_is_refused_and_named(self):
        db.session.add(M3uAccount(name='Provider Two', status='SYNCING',
                                  m3u_url='http://provider.test/playlist.m3u8'))
        db.session.commit()
        resp, popen = self.post()
        self.assertEqual(resp.status_code, 409)
        blocking = resp.get_json().get('blocking', [])
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['status'], 'SYNCING')
        self.assertIn('Provider Two', blocking[0]['label'])
        popen.assert_not_called()

    def test_force_restarts_despite_a_health_check_and_a_sync(self):
        """Every new blocker stays force-able from the GUI - "Restart anyway" is the whole
        reason blocking by default is affordable."""
        seed.make_test_job(name='Nightly guide check', status='RUNNING')
        db.session.add(M3uAccount(name='Provider Two', status='SYNCING',
                                  m3u_url='http://provider.test/playlist.m3u8'))
        db.session.commit()
        resp, popen = self.post({'force': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['forced'])
        popen.assert_called_once()

    def test_idle_restarts_without_a_prompt(self):
        seed.make_recording(status='COMPLETED')
        db.session.commit()
        resp, popen = self.post()
        self.assertEqual(resp.status_code, 200)
        popen.assert_called_once()
        # Always --force: the route is the enforcement point, and a refusal by the
        # script's own guard would be silent.
        self.assertIn('--force', popen.call_args[0][0])


class RestartApiDockerTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-14 "The in-app Restart button was never adapted
    for the Docker container". Inside a container the app IS tini's monitored child
    (docker/entrypoint.sh execs straight into it), so shelling out to restart.sh's
    pkill would make tini exit and tear the whole container down. With
    CHANNELBIN_DOCKER set, the route must self-signal instead of spawning restart.sh,
    while still honouring the same busy guard as the non-container path."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self._env_patch = patch.dict(os.environ, {'CHANNELBIN_DOCKER': '1'})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self.t.cleanup()

    def post(self, body=None):
        with patch('app.routes.settings.subprocess.Popen') as popen, \
             patch('app.routes.settings._schedule_self_restart') as schedule:
            resp = self.t.client.post('/api/settings/restart',
                                      json=body if body is not None else {})
        return resp, popen, schedule

    def test_idle_restart_self_signals_instead_of_shelling_to_restart_sh(self):
        seed.make_recording(status='COMPLETED')
        db.session.commit()
        resp, popen, schedule = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        popen.assert_not_called()
        schedule.assert_called_once()

    def test_busy_guard_still_refuses_in_docker(self):
        seed.make_recording(status='CONVERTING', name='busy in docker')
        db.session.commit()
        resp, popen, schedule = self.post()
        self.assertEqual(resp.status_code, 409)
        popen.assert_not_called()
        schedule.assert_not_called()

    def test_force_self_signals_despite_in_flight_work(self):
        seed.make_recording(status='CONVERTING', name='converting one')
        db.session.commit()
        resp, popen, schedule = self.post({'force': True})
        self.assertEqual(resp.status_code, 200)
        popen.assert_not_called()
        schedule.assert_called_once()


class OrphanedChannelTestReconcileTests(unittest.TestCase):
    """Guards dev/docs/BUGS.md 2026-08-18 "a channel test interrupted by a restart stayed
    open forever".

    Two things depend on this. The visible one: an open row shadowed the channel's real
    latest result and rendered as a bare "Failed" with no reason, for a probe that never
    finished. The structural one: it is what lets tools/check_busy.py block on an open row
    at all - a signal that never self-heals would refuse every restart after the first hard
    kill, forever (dev/changelog/732).
    """

    def setUp(self):
        # Real jobstore needed - resume_in_progress_recordings schedules on-demand test
        # jobs later in the same function.
        self.t = make_test_app(start_scheduler=True)
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Channel Under Test')

    def tearDown(self):
        self.t.cleanup()

    def _resume(self):
        from app.scheduler import resume_in_progress_recordings
        resume_in_progress_recordings(self.t.app)
        db.session.expire_all()

    def test_an_open_test_row_is_closed_as_cancelled_with_a_reason(self):
        test = seed.make_channel_test(self.ch, test_ended_at=None)
        db.session.commit()
        test_id = test.id

        self._resume()

        row = db.session.get(ChannelTest, test_id)
        self.assertIsNotNone(row.test_ended_at)
        self.assertEqual(row.status, TEST_STATUS_CANCELLED)
        # A FAILED/ERROR state with no stated reason is what made the original defect
        # unreadable on the channel page.
        self.assertIn('restart', (row.error_detail or '').lower())

    def test_cancelled_not_failed_so_the_channel_is_not_penalized(self):
        """health_score.py does not score a CANCELLED test. Closing these as FAILED would
        apply the fail floor to a channel nothing is wrong with - the same call the
        preempted-test path already makes (dev/docs/BUGS.md 2026-07-20)."""
        from app.health_score import score_test_quality
        test = seed.make_channel_test(self.ch, test_ended_at=None)
        db.session.commit()
        test_id = test.id

        self._resume()

        row = db.session.get(ChannelTest, test_id)
        self.assertIsNone(score_test_quality(row, {}))

    def test_a_finished_test_is_left_alone(self):
        """The sweep must key on the open row, not on every test the channel ever had."""
        ended = datetime(2026, 8, 1, 12, 0, 0)
        test = seed.make_channel_test(self.ch, status='COMPLETED', test_ended_at=ended)
        db.session.commit()
        test_id = test.id

        self._resume()

        row = db.session.get(ChannelTest, test_id)
        self.assertEqual(row.status, 'COMPLETED')
        self.assertEqual(row.test_ended_at, ended)

    def test_the_guard_stops_blocking_once_startup_has_reconciled(self):
        """The whole point of (f): a hard kill mid-test must not leave check_busy.py
        refusing every restart from then on."""
        seed.make_channel_test(self.ch, test_ended_at=None)
        db.session.commit()
        self.assertEqual(run_check_busy(self.t.db_path).returncode, 1)

        self._resume()

        proc = run_check_busy(self.t.db_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
