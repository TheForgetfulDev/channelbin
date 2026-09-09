"""make_test_app(): the one throwaway-app builder for DB-backed tests.

Every Tier 2 test uses this instead of hand-rolling Flask()+db.init_app so there is a
single, safe app-building path. Guarantees:
  * a fresh temp SQLite file per app (never the live dvr.db),
  * config overrides deep-merged on top of config.yaml via create_app(config_overrides=…),
  * config.yaml itself is sandboxed to an empty temp file for the app's whole lifetime, so
    a runtime load_config()/_load_config_file() call - direct, or via any route - reads
    _DEFAULTS, never the developer's real repo-root file (see the _CONFIG_PATH patch below),
  * the real APScheduler/jobstore/resume machinery skipped (start_scheduler=False),
  * output dirs (dvr, thumbnails, screenshots) and the log file redirected into a temp dir,
  * the ERROR+ alert-log handler stripped so tests don't spawn Alert rows or stack handlers
    across repeated make_test_app() calls,
  * app/'s process-global mutable state reset (see reset_module_globals) so the previous
    test module cannot decide this one's assertions.

The schema is copied from a per-process template DB rather than rebuilt (see
`_template_db_path`), which is why this is ~130ms per app and not ~340ms. Pass
`fresh_schema=True` if a test is specifically about the from-scratch create_all path.

Usage (unittest):

    class MyTest(unittest.TestCase):
        def setUp(self):
            self.t = make_test_app()
        def tearDown(self):
            self.t.cleanup()

        def test_thing(self):
            resp = self.t.client.get('/')
            ...

The app_context is already pushed, so `Model.query` / `db.session` work directly in the
test body; `self.t.client` is a Flask test client.
"""
import atexit
import contextlib
import logging
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app import create_app, db  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app.config import _deep_merge  # noqa: E402
import yaml  # noqa: E402

# Captured at import time, before any test file gets a chance to monkeypatch _CONFIG_PATH -
# this is the one stable reference for "nobody has sandboxed config.yaml yet" that
# TestApp.__init__ below compares against. See its own comment for why.
_REAL_CONFIG_PATH = cfgmod._CONFIG_PATH

# Schema template: built once per process, copied per test. See _template_db_path().
_template_lock = threading.Lock()
_template_dir = None
_template_path = None


def _template_db_path():
    """Path to a process-wide template DB carrying the finished schema, built on demand.

    db.create_all() emits 39 DDL statements across 20 tables and cost ~110ms of every
    single make_test_app() call - roughly a third of the suite's wall time, spent
    rebuilding a schema that is byte-identical every time. So build it once and let each
    test start from a copy (~0.5ms for a 200KB file).

    The template is produced by a real create_app() run (fresh_schema=True below), so it
    carries exactly what a from-scratch build produces - schema, the CURRENT_SCHEMA_VERSION
    user_version stamp, the pinned system health job, and the seeded default tags. Copying
    it in before create_app() therefore changes nothing a test can observe: create_all()
    finds every table present and emits no DDL, and run_migrations() finds the stamp already
    current and returns without running a step.

    Two consequences worth knowing before you touch this:
      * `is_fresh_db()` is False for a preseeded app, so the fresh-DB branch of
        run_migrations() no longer runs. A test that is *about* that branch must pass
        fresh_schema=True (tests/test_migrations_runner.py::FreshDbStampTests does).
      * The template must never be handed to a test directly - each test gets its own copy,
        or one test's writes would bleed into the next.

    **Snapshotted with VACUUM INTO, never shutil.copyfile.** The builder app is still live and
    the database is in WAL mode, so most of what it just wrote is in `dvr.db-wal` rather than
    in `dvr.db` - a plain file copy of the main file alone captured a 4KB stub with no schema
    and no seed rows in it. VACUUM INTO is SQLite's own "write a complete, consistent copy",
    it is what app/migrations.py already uses to snapshot before a migration, and it preserves
    the `user_version` stamp that run_migrations() reads. This copy used to work only by
    accident of ordering - WAL was not switched on until after the schema had been written -
    and stopped the moment that ordering changed (dev/changelog/423).
    """
    global _template_dir, _template_path
    with _template_lock:
        if _template_path is not None:
            return _template_path
        _template_dir = tempfile.mkdtemp(prefix='dvr_test_template_')
        path = os.path.join(_template_dir, 'schema.db')
        builder = TestApp(fresh_schema=True)
        try:
            _snapshot_db(builder, path)
        finally:
            builder.cleanup()
        _template_path = path
        atexit.register(shutil.rmtree, _template_dir, True)
        return _template_path


def _snapshot_db(source_app: 'TestApp', dest_path: str):
    """Write a complete copy of a live TestApp's database to dest_path.

    The session is released first so no read transaction is holding the WAL open, then
    VACUUM INTO does the copy - one statement, and the only correct way to duplicate a
    database with an active write-ahead log. See _template_db_path for why.
    """
    from sqlalchemy import text
    db.session.remove()
    with source_app.app.app_context():
        with db.engine.connect() as conn:
            conn.execute(text('VACUUM INTO :dest'), {'dest': dest_path})


def _strip_alert_handlers():
    """Remove the in-app ERROR+ alert log handler(s) from the root logger.

    _setup_logging() attaches one on every create_app() call; in a test process that
    would (a) write Alert rows into the temp DB on any logged error and (b) stack up
    across repeated make_test_app() calls. Neither is wanted, so pull them here.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if type(h).__name__ == '_AlertHandler':
            root.removeHandler(h)


# Set only by deliberately_missing_config() below. Module-level rather than a TestApp
# argument because the state it describes is process-global (app.config._CONFIG_PATH) and
# is established before any TestApp exists.
_missing_config_is_deliberate = False


@contextlib.contextmanager
def deliberately_missing_config():
    """Declare that _CONFIG_PATH pointing at a nonexistent file is this test's fixture.

    A missing config.yaml is a real state the app has behavior for - it alerts on an
    established install and stays silent on a fresh one (dev/changelog/185) - so a test
    has to be able to build an app in it. Everything else that reaches TestApp with a
    missing _CONFIG_PATH is a patch some earlier test never restored, which TestApp
    refuses by name. Only this makes the difference between the two declarable, so the
    guard can stay strict.
    """
    global _missing_config_is_deliberate
    was = _missing_config_is_deliberate
    _missing_config_is_deliberate = True
    try:
        yield
    finally:
        _missing_config_is_deliberate = was


def write_sandbox_config(path, data):
    """Write a test's sandboxed config.yaml the way production writes the real one.

    A plain `open(path, 'w')` truncates first and dumps after, so any concurrent
    `load_config()` in that window parses a half-written file and raises a ruamel
    ParserError - out of whatever thread happened to be reading (an APScheduler worker, a
    watchdog, a check-window dispatch thread), about a file no test edited. The suite runs
    real background threads, so the window is real: it produced an unexplained
    `ParserError: while parsing a flow node` in test_contention's setUp under a sharded
    run (dev/changelog/724). Production forbids the same shape for the same reason
    (CLAUDE.md "config.yaml is written atomically, under one lock", dev/changelog/671).

    Reuses app/config.py's own primitives rather than restating them - temp file in the
    target's OWN directory (os.replace() is atomic only within one filesystem), fsync,
    permissions carried forward, atomic replace - so a reader sees the old file or the new
    one and never a torn one. The parsed-config cache is dropped inside the lock, so no
    reader can cache pre-write content under a post-write stat key.
    """
    with cfgmod.config_write_lock:
        fd, tmp_path = cfgmod._config_tmp_file(path)
        try:
            with os.fdopen(fd, 'w') as f:
                yaml.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            cfgmod._finish_replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        cfgmod._yaml_cache = None


def reset_module_globals():
    """Return app/'s process-global mutable state to its pristine value.

    These outlive TestApp.cleanup() because they are module attributes, not app or DB
    state: nothing about tearing down an app touches them. A test module that leaves one
    dirty then decides the NEXT module's assertions, which is a real defect and not a
    theoretical one - it is invisible under today's alphabetical ordering and produced 5
    false failures the first time the suite was run in a different module order
    (dev/docs/BUGS.md 2026-08-11, dev/changelog/583).

    Called on the way IN rather than from cleanup(), so a test is protected no matter who
    dirtied the process - including code that ran outside a TestApp entirely, and including
    a test that crashed before its own cleanup.

    Every module-level mutable global in app/ is either reset here or listed in
    tests/test_global_state_isolation.py's allowlist with a reason. That test is what keeps
    this list from rotting the next time one is added.
    """
    import app.admission as admission
    import app.auth as auth
    import app.channel_search as channel_search
    import app.channel_tester as channel_tester
    import app.config as config
    import app.fs_utils as fs_utils
    import app.routes.channel_search as channel_search_routes
    import app.search_index as search_index

    # A run abandoned mid-flight leaves its admission ticket held, and the next module's
    # sync or maintenance job is then refused by a blocker that no longer exists.
    admission.reset_for_tests()
    # A health-check run left mid-flight reports is_running() True forever after, so the
    # next module's job rows read RUNNING instead of SCHEDULED.
    channel_tester._state = channel_tester.RunState()
    # rebuild_in_progress() True makes the dashboard's background-activity indicator read
    # 'active' when the next module expects 'hidden'.
    search_index._rebuilding = False
    # The janitor's stale clock is keyed by index name only, so an entry left by a previous
    # module makes this module's first tick read as already past its grace window - i.e. an
    # unexpected rebuild where the test set up a fresh observation.
    search_index._stale_since.clear()
    # Lockout counters are keyed by IP, and every test client is 127.0.0.1.
    auth._failures = {}
    # The disk-readout warning fires only on a change of outcome, so an entry left by a
    # previous module makes this module's first probe of the same path look unchanged -
    # i.e. no warning where the test set up a freshly-unreachable mount.
    fs_utils._last_logged_outcome.clear()
    # save_config() latches this for any RESTART_REQUIRED_KEYS write, so one module's
    # settings save leaves the next module's pages rendering a restart-required banner.
    config._restart_needed = False
    # A tag id is only unique within one test's temp DB, so a stale cache entry from a
    # previous module's tag with the same id would serve the wrong channel-id set here.
    channel_search._tag_channel_ids_cache.clear()
    # Same reasoning, plus one of its own: this cache expires on a wall clock rather than on a
    # watermark, so an entry from a previous module is served to this one for up to a minute -
    # long enough to outlive several test methods.
    channel_search._now_tag_channel_ids_cache.clear()
    # Same reasoning as the tag cache above: the standing-breakdown cache's key carries no
    # per-test identity, so a stale entry from a previous module's temp DB would otherwise
    # serve the wrong total/hidden counts here.
    channel_search._standing_breakdown_cache.clear()
    # Search-session ids are supplied by the caller, so two modules that both use a short
    # literal ('page-1') would share an entry - and a leftover high seq makes the next
    # module's first request read as already superseded, i.e. a 409 where it expects rows.
    # Not in test_global_state_isolation.py's RESET map on purpose: that map is checked
    # against a scan that only sees `global` rebinds and empty container literals, and this
    # is an object, so naming it there would fail the "RESET still matches the source" test.
    channel_search_routes.SEARCH_GENERATIONS.reset()

    # NOT reset: channel_tester._log_seq. It is documented as monotonic for the process
    # lifetime (_append_log relies on it never going backwards), so resetting it would
    # break the thing it exists to guarantee. It is append-only and carries no state a
    # later test can read wrongly.


def _assert_jobstore_is_sandboxed(tmpdir):
    """Fail loudly if the live scheduler's jobstore isn't pointed at the temp DB.

    A test process must never be able to write apscheduler_jobs rows into the real
    dvr.db: those rows reference test-module functions, and production then fails to
    reconstitute them at every startup (BUGS.md 2026-07-18). The ORM is sandboxed via
    create_app(config_overrides=…), but the jobstore builds its own engine - so it gets
    its own check here rather than being assumed to follow.
    """
    import app.scheduler as sched
    url = str(sched._scheduler._lookup_jobstore('default').engine.url)
    if tmpdir not in url:
        sched._scheduler.shutdown(wait=False)
        raise AssertionError(
            f'APScheduler jobstore escaped the test sandbox: {url!r} is not under {tmpdir!r}. '
            'init_scheduler() must build its engine from app.config[\'SQLALCHEMY_DATABASE_URI\'].'
        )


class TestApp:
    """A live test app + client bound to a temp DB and temp dirs. Call cleanup() when done."""

    def __init__(self, extra_overrides=None, start_scheduler=False, fresh_schema=False):
        reset_module_globals()
        self._tmpdir = tempfile.mkdtemp(prefix='dvr_test_')
        self.db_path = os.path.join(self._tmpdir, 'dvr.db')
        dvr_dir = os.path.join(self._tmpdir, 'dvr')
        thumb_dir = os.path.join(self._tmpdir, 'thumbnails')
        shot_dir = os.path.join(self._tmpdir, 'screenshots')
        # Capture stderr spools (dev/changelog/430). Sandboxed like every other output dir:
        # its default is app-root-relative, so without this override a test that reaches
        # _launch_segment would spool into the real repo.
        cap_log_dir = os.path.join(self._tmpdir, 'capture-logs')
        for d in (dvr_dir, thumb_dir, shot_dir, cap_log_dir):
            os.makedirs(d, exist_ok=True)

        overrides = {
            'database': {
                'path': self.db_path,
                # No migration snapshots in tests: the temp DB is created fresh by
                # create_all(), so there is never a pending step to back up.
                'pre_migration_backup': False,
                'backup_dir': os.path.join(self._tmpdir, 'db-backups'),
            },
            'recording': {
                'dvr_output_dir': dvr_dir,
                'capture_log_dir': cap_log_dir,
                'live_thumbnail': {'dir': thumb_dir},
            },
            'channel_testing': {'screenshot_dir': shot_dir},
            'config_backup': {'backup_dir': os.path.join(self._tmpdir, 'config-backups')},
            'logging': {'file': os.path.join(self._tmpdir, 'test.log')},
            # No real webhooks ever fire from a test.
            'notifications': {'base_url': ''},
            # Own private pidfile per TestApp, not the real repo's instance/channelbin.pid -
            # otherwise every start_scheduler=True test across every sharded worker process
            # would fight over the same file and spuriously trip the second-instance guard
            # (app/scheduler.py::init_scheduler, dev/docs/BUGS.md 2026-08-14).
            'flask': {'pidfile_path': os.path.join(self._tmpdir, 'channelbin.pid')},
        }
        if extra_overrides:
            overrides = _deep_merge(overrides, extra_overrides)

        # Preseed the schema unless this app IS the template build, or the caller asked
        # for the real from-scratch path. The path check keeps the copy honest: an
        # extra_overrides that redirected database.path would otherwise seed a file
        # create_app() never opens, silently reverting this app to create_all().
        if not fresh_schema and overrides['database']['path'] == self.db_path:
            shutil.copyfile(_template_db_path(), self.db_path)

        # Sandbox config.yaml itself, not just the ORM/output-dir overrides above. Without
        # this, config_overrides only reaches create_app()'s own one-time cfg variable - any
        # OTHER runtime load_config() call (a route, a scheduled job, a background thread)
        # re-reads the real repo-root file, which is how three tests silently depended on
        # whatever the developer's own config.yaml happened to contain (dev/docs/BUGS.md
        # 2026-08-08 08:27 PM). tests/support/config_sandbox.py::ConfigSandbox already fixed
        # this per-test, opt-in; this makes it the unconditional default so no test can skip it.
        #
        # Cooperative, not unconditional: only install our own sandbox if _CONFIG_PATH still
        # points at the real file. A test that has already patched it away (ConfigSandbox, or
        # a hand-rolled equivalent - test_config_cache.py, test_ha_auth.py, etc. all do this in
        # their own setUp before calling make_test_app()) owns that file's content on purpose
        # (a specific HA API key, a comment-preservation fixture, a deliberately-missing path);
        # blindly overwriting it here clobbers that fixture instead of protecting anything -
        # confirmed empirically: an unconditional version of this patch broke exactly those 20
        # tests, all in files that already sandbox themselves, and none of them a genuine
        # dependency on the real file.
        #
        # A _CONFIG_PATH that is neither the real file nor an existing one is normally
        # nobody's live sandbox - it is a patch some earlier test never restored (a setUp
        # that raises after patching skips its own tearDown entirely). Left unnamed it
        # turns every later test in the process into a mystery: this app defers to the
        # stale path, sandbox_config() cannot write, and load_config() quietly serves
        # _DEFAULTS. A test that is ABOUT the missing-config path says so with
        # deliberately_missing_config() rather than being guessed at from the outside.
        if (cfgmod._CONFIG_PATH != _REAL_CONFIG_PATH
                and not _missing_config_is_deliberate
                and not os.path.exists(cfgmod._CONFIG_PATH)):
            raise AssertionError(
                f'app.config._CONFIG_PATH points at {cfgmod._CONFIG_PATH}, which does not '
                'exist - an earlier test patched it and never put it back, so every config '
                'read in this process is now serving defaults. Find that test rather than '
                'working around it here.')
        self._cfg_path = None
        if cfgmod._CONFIG_PATH == _REAL_CONFIG_PATH:
            self._orig_cfg_path = cfgmod._CONFIG_PATH
            fd, self._cfg_path = tempfile.mkstemp(suffix='.yaml', prefix='dvr_test_cfg_')
            os.close(fd)
            # Stamped at the current config_version, not left empty: an unstamped file
            # makes migrate_config() think it needs a pre-migration backup on every single
            # test-app build, which used to escape the sandbox entirely (dev/changelog/620).
            write_sandbox_config(self._cfg_path,
                                 {'config_version': cfgmod.CURRENT_CONFIG_VERSION})
            cfgmod._CONFIG_PATH = self._cfg_path
            cfgmod._yaml_cache = None

        self.app = create_app(config_overrides=overrides, start_scheduler=start_scheduler)
        self._started_scheduler = start_scheduler
        if start_scheduler:
            _assert_jobstore_is_sandboxed(self._tmpdir)
        _strip_alert_handlers()
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()

    def sandbox_config(self, overrides):
        """Write `overrides` into this app's sandboxed config.yaml so a runtime
        load_config() call - a background thread or scheduled job re-importing it
        locally, not just create_app()'s one-time config_overrides - sees them too
        (CLAUDE.md's Testing section: overrides passed to make_test_app() are otherwise
        invisible to any load_config() call made after startup).

        Raises if this app never got its own sandbox (see __init__'s 'cooperative, not
        unconditional' note) - the request cannot be honored, and returning quietly is
        worse than failing. A silent no-op here means the test reads pure _DEFAULTS
        instead of what it just asked for, so it fails much later on a value that looks
        like an application bug: the capture-scratch-dir test failed exactly this way,
        with `None != '/tmp/dvr_test_.../custom_scratch'` and nothing pointing at the
        config sandbox (dev/changelog/724)."""
        if self._cfg_path is None:
            raise AssertionError(
                'sandbox_config() cannot write: this TestApp deferred to an outer config '
                f'sandbox already pointing app.config._CONFIG_PATH at {cfgmod._CONFIG_PATH}. '
                'Either write the values into that sandbox instead (ConfigSandbox._write_cfg), '
                'or find the test that patched _CONFIG_PATH and never restored it.')
        data = {'config_version': cfgmod.CURRENT_CONFIG_VERSION}
        data.update(overrides)
        write_sandbox_config(self._cfg_path, data)

    def cleanup(self):
        try:
            db.session.remove()
        finally:
            try:
                self._teardown_active_recordings()
                from .notifyguard import cancel_pending
                cancel_pending()
            finally:
                try:
                    self._shutdown_scheduler()
                finally:
                    try:
                        self.ctx.pop()
                    finally:
                        try:
                            shutil.rmtree(self._tmpdir, ignore_errors=True)
                        finally:
                            if self._cfg_path is not None:
                                cfgmod._CONFIG_PATH = self._orig_cfg_path
                                cfgmod._yaml_cache = None
                                if os.path.exists(self._cfg_path):
                                    os.remove(self._cfg_path)
                            self._assert_no_network_attempts()

    @staticmethod
    def _assert_no_network_attempts():
        """Fail the test if anything tried to reach the network during this app's life.

        The guard in tests/support/netguard.py already blocks the call, but a block on a
        background thread (an APScheduler worker, a watchdog) gets swallowed by the broad
        error handling in the production code that made the call - the recording just goes
        FAILED and the test still passes. Draining here is what makes the violation
        surface as a test failure instead of a silent no-op.
        """
        from .netguard import drain_violations
        found = drain_violations()
        if found:
            raise AssertionError(
                f'{len(found)} network access attempt(s) were blocked during this test '
                f'(see CLAUDE.md §Testing - the suite must never reach the network):\n\n'
                + '\n\n'.join(found))

    @staticmethod
    def _teardown_active_recordings():
        """Kill anything left in recorder._active before this app goes away.

        A test that reaches start_recording() (directly, or via a scheduled job that
        fires during the test) leaves a live ffmpeg child and a WatchdogThread behind.
        Those are daemon threads, so nothing reaps them: the watchdog keeps polling
        load_config() about once a second and keeps relaunching ffmpeg for the rest of
        the process. That leak is what made the config-parse scaling guards flake
        (tests/support/iocount.py), and it is why a stale test could spawn ffmpeg at a
        network URL long after the test that created it had passed.

        Deliberately in cleanup() rather than in any one test: no test should have to
        remember this, and the next one to start a recording by accident is covered too.
        """
        import app.recorder as recorder
        from app.proc_utils import terminate_or_kill

        with recorder._lock:
            states = list(recorder._active.items())
            recorder._active.clear()

        for _rid, state in states:
            state.stop_event.set()
            if state.process is not None:
                try:
                    terminate_or_kill(state.process, hard=True)
                except OSError:
                    pass  # already reaped; nothing to kill
            if state.watchdog is not None:
                state.watchdog.join(timeout=5)
            # A pending relaunch after a failed spawn waits on the same stop_event, so it
            # is already unblocked above; joining keeps it from outliving the app whose
            # context it would push (app/recorder.py::_schedule_launch_retry).
            if getattr(state, 'launch_retry', None) is not None:
                state.launch_retry.join(timeout=5)

    def _shutdown_scheduler(self):
        """Stop the BackgroundScheduler this TestApp started and clear the module globals.

        Without this each start_scheduler=True setUp leaks a live scheduler thread that
        keeps polling a jobstore whose DB file is about to be deleted, and leaves
        app.scheduler._scheduler/_app pointing at a torn-down app for the next test.
        """
        if not self._started_scheduler:
            return
        import app.scheduler as sched
        from apscheduler.schedulers.base import SchedulerNotRunningError
        try:
            sched._scheduler.shutdown(wait=False)
        except (SchedulerNotRunningError, AttributeError):
            pass  # already stopped, or never got far enough to be assigned
        sched._scheduler = None
        sched._app = None


def make_test_app(extra_overrides=None, start_scheduler=False, fresh_schema=False):
    """Build and return a TestApp (temp DB, scheduler off, dirs/log redirected).

    extra_overrides: nested dict deep-merged on top of the built-in test overrides
        (e.g. to flip a feature flag for one test).
    start_scheduler: opt back into the real APScheduler/jobstore - only the contention
        suite needs this (against its own temp DB).
    fresh_schema: build the schema with db.create_all() instead of copying the process
        template (_template_db_path). Only for tests asserting on the from-scratch path
        itself - it costs ~110ms more, and every other test observes the same DB either way.
    """
    return TestApp(extra_overrides=extra_overrides, start_scheduler=start_scheduler,
                   fresh_schema=fresh_schema)
