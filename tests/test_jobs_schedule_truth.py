"""/jobs and the Dashboard timeline report the truth about what is scheduled (dev/changelog/1258).

Guards dev/docs/BUGS.md 2026-10-21 "The Dashboard timeline called every job's runtime
unknown and missed half the deferrals it exists to show". Invariants:

  (a) Every job id app/scheduler.py can register is named on /jobs - no row shows a raw
      job id. The scan below fails when a new `_add_job` appears that this file has no
      sample for, so a new job kind cannot reach the page unnamed.
  (b) The Dashboard timeline carries the same runtime estimate /jobs shows, instead of
      calling every job's runtime unknown.
  (c) A sync whose start falls inside a recording that is still SCHEDULED now is marked as
      deferred - it will be capturing when the sync fires, which is the guard's commonest
      case.
  (d) A scheduled health check is marked when a recording starts within its lead time, and
      the capturing guard marks only the automatic TV Guide Channels check.
  (e) A lead time of 0 marks nothing: the scheduler treats 0 as off.
  (f) A full health check run records its run history under its scheduler id, so the
      deferral past a recording, /jobs and the timeline have an estimate to read; a partial
      (resumed) run does not; deleting the check removes its history.
  (g) A retry row reads its base job's estimate - the retry runs that job's body.

The same entry's Run now toast (an EPG refresh admission held back reported as "started")
is guarded by tests/test_jobs_page_js.py::RunNowRefusedTests, against the shipped jobs.js.

  python3 -m unittest tests.test_jobs_schedule_truth
"""
import ast
import copy
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from apscheduler.triggers.interval import IntervalTrigger

from app import channel_tester, db
from app import scheduler as scheduler_mod
from app.database import JobRun, OnDemandTestJob, record_job_run, JOB_RUN_SUCCESS
from tests.support import seed
from tests.support.app import make_test_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
TIMELINE_HARNESS = os.path.join(REPO, 'tests', 'support', 'dashboard_timeline.mjs')

# (enclosing function, id expression) for every _add_job call in app/scheduler.py, mapped to
# the ids it produces. `{rec}`, `{acc}`, `{src}`, `{od}` are filled with seeded row ids.
# `_defer_job_for_contention` is absent on purpose: its ids are derived from its callers.
SAMPLE_IDS = {
    ('schedule_recording', "f'start_{recording_id}'"): ['start_{rec}'],
    ('schedule_recording', "f'stop_{recording_id}'"): ['stop_{rec}', 'stop_{live}'],
    ('schedule_dead_stream_retry', "f'retry_{recording_id}'"): ['retry_{rec}'],
    ('_schedule_precheck_job', "f'precheck_{recording_id}'"): ['precheck_{rec}'],
    ('reschedule_precheck', "f'precheck_{recording_id}'"): ['precheck_{rec}'],
    ('reschedule_recording_start', "f'start_{recording_id}'"): ['start_{rec}'],
    ('reschedule_recording_resume', "f'resume_{recording_id}'"): ['resume_{live}'],
    ('reschedule_recording_stop', "f'stop_{recording_id}'"): ['stop_{rec}'],
    ('schedule_on_demand_job', '**'): ['od_job_{od}'],
    ('schedule_on_demand_job', 'aps_job_id'): ['od_job_{od}'],
    ('schedule_account_sync', 'job_id'): ['account_sync_{acc}'],
    ('schedule_epg_source_refresh', 'job_id'): ['epg_source_refresh_{src}'],
    ('defer_source_refresh', 'epg_source_retry_job_id(source_id)'):
        ['epg_source_refresh_retry_{src}'],
    ('_defer_sync_past_recording', 'sync_retry_job_id(account_id)'):
        ['account_sync_retry_{acc}'],
    ('_defer_sync_for_contention', 'sync_retry_job_id(account_id)'):
        ['account_sync_retry_{acc}'],
    ('defer_health_check_past_recording', 'health_check_retry_job_id(job_id)'):
        ['od_job_retry_{od}'],
    ('defer_hide_materialize', 'HIDE_MATERIALIZE_RETRY_JOB_ID'):
        ['channel_hide_materialize_retry'],
    ('queue_auto_rule_evaluation', 'AUTO_RULES_EVALUATE_JOB_ID'): ['auto_rules_evaluate'],
    ('schedule_auto_rule_sweep', 'AUTO_RULE_SWEEP_JOB_ID'): ['auto_rule_sweep'],
    ('_register_window_close_job', "'hc_window_close'"): ['hc_window_close'],
    ('schedule_check_window_jobs', "'hc_window_dispatch'"): ['hc_window_dispatch'],
    ('schedule_config_backup', "'config_backup_daily'"): ['config_backup_daily'],
    ('schedule_recording_retention', "'recording_retention_daily'"):
        ['recording_retention_daily'],
    ('schedule_db_maintenance', "'db_maintenance_daily'"): ['db_maintenance_daily'],
    ('apply_logo_cache_schedule', "'logo_cache_fetch'"): ['logo_cache_fetch'],
    ('schedule_index_janitor', "'search_index_janitor'"): ['search_index_janitor'],
    ('schedule_storage_dirs_check', "'storage_dirs_check'"): ['storage_dirs_check'],
    ('schedule_host_resolve_check', "'host_resolve_check'"): ['host_resolve_check'],
    ('schedule_account_stats_fold', "'account_stats_fold'"): ['account_stats_fold'],
}


def _scheduler_add_job_sites():
    """Every _add_job call in app/scheduler.py as (enclosing function, id expression), plus
    the base ids _defer_job_for_contention is called with."""
    with open(os.path.join(REPO, 'app', 'scheduler.py'), encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    sites, contention_bases = set(), set()
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', None)
            if name == '_add_job':
                ids = [k for k in node.keywords if k.arg == 'id']
                sites.add((fn.name, ast.unparse(ids[0].value) if ids else '**'))
            elif name == '_defer_job_for_contention' and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    contention_bases.add(first.value)
    return sites, contention_bases


class _FakeJob:
    def __init__(self, job_id, next_run, trigger=None, kwargs=None):
        self.id = job_id
        self.next_run_time = next_run.replace(tzinfo=timezone.utc)
        self.trigger = trigger or IntervalTrigger(hours=1)
        self.kwargs = kwargs or {}


def _fake_scheduler(jobs):
    return types.SimpleNamespace(get_jobs=lambda: list(jobs))


class EveryScheduledJobIsNamedTests(unittest.TestCase):
    """(a)"""

    def test_every_add_job_site_has_a_sample(self):
        sites, bases = _scheduler_add_job_sites()
        sites.discard(('_defer_job_for_contention', "f'{job_id}_retry'"))
        missing = sorted(sites - set(SAMPLE_IDS))
        self.assertEqual(missing, [], 'app/scheduler.py registers a job this test has no '
                         'sample id for - add one to SAMPLE_IDS and make /jobs name it')
        self.assertTrue(bases, '_defer_job_for_contention has no literal callers left')

    def test_no_row_shows_a_raw_job_id(self):
        from app.routes.jobs import _build_job_list
        t = make_test_app()
        try:
            acc = seed.make_account(name='Acct')
            ch = seed.make_channel(acc)
            src = seed.make_epg_source(acc)
            now = datetime.utcnow()
            rec = seed.make_recording(status='SCHEDULED', name='Booked', channel_id=ch.id,
                                      start_time=now + timedelta(hours=2),
                                      stop_time=now + timedelta(hours=3))
            live = seed.make_recording(status='IN_PROGRESS', name='Live', channel_id=ch.id)
            od = seed.make_test_job(name='Nightly', status='SCHEDULED', recurring=True,
                                    recur_day=0, recur_hour=2, recur_minute=0)
            db.session.commit()
            fill = dict(rec=rec.id, live=live.id, acc=acc.id, src=src.id, od=od.id)
            _sites, bases = _scheduler_add_job_sites()
            ids = {i.format(**fill) for samples in SAMPLE_IDS.values() for i in samples}
            ids |= {f'{b}_retry' for b in bases}
            jobs = [_FakeJob(job_id, now + timedelta(minutes=10 + n))
                    for n, job_id in enumerate(sorted(ids))]
            with mock.patch.object(scheduler_mod, 'get_scheduler',
                                   return_value=_fake_scheduler(jobs)):
                items = _build_job_list()
        finally:
            t.cleanup()
        row_ids = {item['id'] for item in items}
        # A recording's stop and side jobs are lines on its row, and the running one's row
        # is named for its state, so those ids are not rows of their own.
        on_recording_rows = {f'stop_{rec.id}', f'precheck_{rec.id}', f'retry_{rec.id}',
                             f'stop_{live.id}', f'resume_{live.id}'}
        self.assertIn(f'active_{live.id}', row_ids)
        for job_id in sorted(ids - on_recording_rows):
            self.assertIn(job_id, row_ids, f'{job_id} has no row on /jobs')
        for item in items:
            self.assertNotIn(item['id'], item['display_name'],
                             f"{item['id']} is shown by its raw id")
            self.assertIn(item['type'], ('recurring', 'one_off', 'active', 'on_event'))


class _JobListCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.now = datetime.utcnow()

    def tearDown(self):
        self.t.cleanup()

    def _items(self, jobs):
        from app.routes.jobs import _build_job_list
        with mock.patch.object(scheduler_mod, 'get_scheduler',
                               return_value=_fake_scheduler(jobs)):
            return {item['id']: item for item in _build_job_list()}


class RetryReadsItsBaseJobsEstimateTests(_JobListCase):
    """(g)"""

    def test_a_maintenance_retry_carries_the_maintenance_estimate(self):
        start = datetime(2026, 1, 1)
        record_job_run('db_maintenance_daily', start, start + timedelta(minutes=6),
                       JOB_RUN_SUCCESS)
        items = self._items([_FakeJob('db_maintenance_daily_retry',
                                      self.now + timedelta(minutes=30))])
        item = items['db_maintenance_daily_retry']
        self.assertEqual(item['est_seconds'], 360)
        self.assertIn('average of the last 1 run', item['duration_line'])


class HealthCheckRunHistoryTests(unittest.TestCase):
    """(f)"""

    def setUp(self):
        self.t = make_test_app()
        self.channel = seed.make_channel(seed.make_account())
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _run(self, completed=True, subset=None):
        job = seed.make_test_job(name='Nightly Check', channels=[self.channel],
                                 status='RUNNING')
        db.session.commit()
        with mock.patch('app.channel_tester.imminent_recording_conflict', return_value=None), \
             mock.patch('app.channel_tester._run_channel_loop', return_value=completed):
            channel_tester.run_on_demand_test_job(self.t.app, job.id,
                                                  channel_id_subset=subset)
        db.session.expire_all()
        return job.id, JobRun.query.filter_by(job_id=f'od_job_{job.id}').all()

    def test_a_full_run_is_recorded_under_its_scheduler_id(self):
        _job_id, runs = self._run()
        self.assertEqual([r.outcome for r in runs], [JOB_RUN_SUCCESS])

    def test_a_run_stopped_short_is_recorded_failed(self):
        _job_id, runs = self._run(completed=False)
        self.assertEqual([r.outcome for r in runs], ['FAILED'])

    def test_a_resumed_run_is_not_recorded(self):
        _job_id, runs = self._run(subset=[self.channel.id])
        self.assertEqual(runs, [])

    def test_the_deferral_sizes_its_gap_from_that_history(self):
        from app.database import get_job_duration_estimate
        job_id, _runs = self._run()
        avg, count = get_job_duration_estimate(f'od_job_{job_id}')
        self.assertEqual(count, 1)
        self.assertIsNotNone(avg)

    def test_deleting_the_check_removes_its_history(self):
        from app.channel_groups import teardown_test_job
        job = seed.make_test_job(name='Doomed', channels=[self.channel])
        db.session.commit()
        job_id = job.id
        start = datetime(2026, 1, 1)
        record_job_run(f'od_job_{job_id}', start, start + timedelta(minutes=4), JOB_RUN_SUCCESS)
        teardown_test_job(db.session.get(OnDemandTestJob, job_id))
        db.session.commit()
        self.assertEqual(JobRun.query.filter_by(job_id=f'od_job_{job_id}').count(), 0)


class TimelinePayloadTests(unittest.TestCase):
    """What _timeline_payload hands the page, per job - DESIGN.md 16.3 and 16.4."""

    def _payload_jobs(self, history):
        from app.routes.dashboard import _timeline_payload
        t = make_test_app()
        try:
            acc = seed.make_account(name='Acct')
            db.session.commit()
            if history:
                start = datetime(2026, 1, 1)
                record_job_run('db_maintenance_daily', start, start + timedelta(minutes=3),
                               JOB_RUN_SUCCESS)
            now = datetime.utcnow()
            jobs = [_FakeJob('db_maintenance_daily', now + timedelta(hours=1)),
                    _FakeJob(f'account_sync_{acc.id}', now + timedelta(hours=2)),
                    _FakeJob('hc_window_dispatch', now + timedelta(hours=3))]
            with mock.patch.object(scheduler_mod, 'get_scheduler',
                                   return_value=_fake_scheduler(jobs)), \
                    _guarded_config(sync_within=7):
                payload = _timeline_payload([], [acc], now)
            return {j['id'].split('_')[0] if j['id'].startswith('account_sync_') else j['id']: j
                    for j in payload['jobs']}
        finally:
            t.cleanup()

    def test_a_job_with_no_history_carries_no_invented_estimate(self):
        """16.3: an outline with no length, never a placeholder number."""
        job = self._payload_jobs(history=False)['db_maintenance_daily']
        self.assertIsNone(job['est_seconds'])
        self.assertIn('Expected runtime unknown', job['duration_line'])

    def test_a_job_with_history_carries_its_estimate(self):
        job = self._payload_jobs(history=True)['db_maintenance_daily']
        self.assertEqual(job['est_seconds'], 180)

    def test_the_guard_thresholds_come_from_config(self):
        """16.4: the marks describe what the scheduler will actually do."""
        jobs = self._payload_jobs(history=False)
        self.assertEqual(jobs['account']['guard'],
                         {'what': 'sync', 'active': True, 'within_minutes': 7})
        self.assertIsNone(jobs['db_maintenance_daily']['guard'])
        self.assertIsNone(jobs['hc_window_dispatch']['duration_line'],
                          'an untracked job claimed a runtime line')


def _guarded_config(sync_within=5, check_within=10, sync_active=True):
    """load_config with known guard values, so the marks under test do not depend on what
    this machine's config.yaml says."""
    from app import config as config_mod
    real = config_mod.load_config

    def fake(*a, **kw):
        cfg = copy.deepcopy(real(*a, **kw))
        cfg.setdefault('sync', {}).update(skip_sync_if_recording_active=sync_active,
                                          skip_sync_if_recording_within_minutes=sync_within)
        cfg.setdefault('channel_testing', {}).update(
            skip_if_recording_active=True, skip_if_recording_within_minutes=check_within)
        return cfg
    return mock.patch.object(config_mod, 'load_config', fake)


def _timeline_tips(setup):
    """Render / with the scheduler stand-in `setup` builds, run the shipped timeline in
    jsdom, and return every tooltip it drew, in drawing order."""
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='timeline_jobs_')
    try:
        with t.app.app_context():
            jobs, patches = setup(datetime.utcnow())
            db.session.commit()
            with mock.patch.object(scheduler_mod, 'get_scheduler',
                                   return_value=_fake_scheduler(jobs)), patches:
                html = t.app.test_client().get('/').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as fh:
            fh.write(html)
        proc = subprocess.run(['node', TIMELINE_HARNESS, tmp, REPO], capture_output=True,
                              text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        out = json.loads(proc.stdout)
        if out['errors']:
            raise AssertionError(out['errors'])
        return out['initial']['tips']
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TimelineMarksTests(unittest.TestCase):
    """(b)-(e), through the route and the shipped dashboard.js."""

    @classmethod
    def setUpClass(cls):
        def setup(now):
            acc = seed.make_account(name='Acct')
            ch = seed.make_channel(acc)
            # A recording booked across the sync's start (c), one starting just after the
            # health check (d), and one starting just after the zero-lead-time sync (e).
            seed.make_recording(status='SCHEDULED', name='Covers sync', channel_id=ch.id,
                                start_time=now + timedelta(minutes=30),
                                stop_time=now + timedelta(minutes=120))
            seed.make_recording(status='SCHEDULED', name='Soon after check', channel_id=ch.id,
                                start_time=now + timedelta(minutes=243),
                                stop_time=now + timedelta(minutes=300))
            seed.make_recording(status='SCHEDULED', name='Covers checks', channel_id=ch.id,
                                start_time=now + timedelta(minutes=400),
                                stop_time=now + timedelta(minutes=500))
            custom = seed.make_test_job(name='Custom check', status='SCHEDULED')
            system = OnDemandTestJob.query.filter_by(is_system=True).first()
            system.status = 'SCHEDULED'
            db.session.flush()
            start = datetime(2026, 1, 1)
            record_job_run('config_backup_daily', start, start + timedelta(minutes=8),
                           JOB_RUN_SUCCESS)
            jobs = [
                _FakeJob(f'account_sync_{acc.id}', now + timedelta(minutes=60)),
                _FakeJob(f'od_job_{custom.id}', now + timedelta(minutes=240)),
                _FakeJob(f'od_job_{system.id}', now + timedelta(minutes=420)),
                _FakeJob(f'od_job_retry_{custom.id}', now + timedelta(minutes=430)),
                _FakeJob('config_backup_daily', now + timedelta(minutes=600)),
            ]
            cls.names = {'sync': 'Sync: Acct', 'custom': 'Health check: Custom check',
                         'system': f'Health check: {system.name}', 'backup': 'Config Backup'}
            return jobs, _guarded_config(sync_within=0, check_within=10)
        cls.tips = _timeline_tips(setup)

    def marks(self, key):
        """Every job mark named for `key`, in time order - a job's first tooltip line is its
        name, then its next run."""
        name = self.names[key]
        return [tip for tip in self.tips
                if tip.split('\n', 1)[0] == name and '\nNext run ' in tip]

    def tip(self, key):
        marks = self.marks(key)
        self.assertTrue(marks, f'no mark for {self.names[key]}: {self.tips}')
        return marks[0]

    def test_a_job_with_history_carries_the_estimate_jobs_shows(self):
        """(b)"""
        self.assertIn('Usually takes about 8 minutes (average of the last 1 run).',
                      self.tip('backup'))

    def test_a_sync_inside_a_booked_recording_is_marked(self):
        """(c)"""
        tip = self.tip('sync')
        self.assertIn('Will be deferred', tip)
        self.assertIn('"Covers sync" will be recording then', tip)

    def test_a_check_with_a_recording_starting_inside_its_lead_time_is_marked(self):
        """(d)"""
        tip = self.tip('custom')
        self.assertIn('Will be deferred', tip)
        self.assertIn('"Soon after check" starts within 10 minutes', tip)

    def test_the_capturing_guard_marks_only_the_automatic_check(self):
        """(d) - the custom check's retry at +430m sits inside "Covers checks" too."""
        self.assertIn('"Covers checks" will be recording then', self.tip('system'))
        custom = self.marks('custom')
        self.assertEqual(len(custom), 2, 'the custom check and its retry')
        self.assertNotIn('Will be deferred', custom[1],
                         'a custom check was marked for the capturing guard')



@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class TimelineZeroLeadTimeTests(unittest.TestCase):
    """(e) - a recording starting at the sync's very moment, with the capturing guard off
    and the lead time 0, is not a deferral the scheduler would make."""

    def test_a_zero_lead_time_marks_nothing(self):
        def setup(now):
            acc = seed.make_account(name='Acct')
            ch = seed.make_channel(acc)
            seed.make_recording(status='SCHEDULED', name='Same moment', channel_id=ch.id,
                                start_time=now + timedelta(minutes=60),
                                stop_time=now + timedelta(minutes=90))
            db.session.flush()
            return ([_FakeJob(f'account_sync_{acc.id}', now + timedelta(minutes=60))],
                    _guarded_config(sync_within=0, sync_active=False))
        tips = [tip for tip in _timeline_tips(setup) if tip.startswith('Sync: Acct\n')]
        self.assertEqual(len(tips), 1, tips)
        self.assertNotIn('Will be deferred', tips[0])


if __name__ == '__main__':
    unittest.main()
