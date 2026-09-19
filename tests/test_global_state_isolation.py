"""Tier 0/2 - app/'s process-global mutable state must not cross test boundaries.

`TestApp.cleanup()` tears down the app, the DB, the temp dirs and anything left in
`recorder._active`, but none of that touches a plain module attribute. A test module that
leaves one dirty therefore decides the *next* module's assertions, and today's alphabetical
ordering is the only reason that has not been visible.

It became visible the moment the suite was run in a different module order (sharded across
worker processes, dev/changelog/583): five tests failed that pass serially and pass alone.
`app/channel_tester.py::_state` left mid-run makes `test_health_check_complete_alert` read
`job.status == 'RUNNING'` where it expects `'SCHEDULED'`; `app/search_index.py::_rebuilding`
left True makes the dashboard's background-activity indicator read `'active'` where
`test_dashboard_activity_search_index` and `test_failure_observability` expect `'hidden'`
(dev/docs/BUGS.md 2026-08-11 06:22 PM).

Two halves, and the second is the one that lasts:

  * The behavioral tests dirty each reset global by hand and assert `make_test_app()` hands
    back a pristine one.
  * `ProcessGlobalCoverageTests` is a static scan of `app/` for process-global mutable
    state - anything rebound through a `global` statement, plus anything bound to an empty
    mutable container at module level - and asserts every single name is either reset by
    `reset_module_globals()` or carries an explicit allowlist entry saying who owns it
    instead. Without that half this fix rots the first time somebody adds a fourth global,
    and the failure it causes lands in an unrelated module three weeks later.

Being on the allowlist is a decision, not an exemption: those names are cleaned up by
something that already knows how to do it safely (TestApp.cleanup, the owning thread's own
finally block, ConfigSandbox), and clearing them here would strand a live thread, process or
queue belonging to an app that is still open.
"""
import ast
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app, reset_module_globals  # noqa: E402

import app.auth as auth  # noqa: E402
import app.channel_search as channel_search  # noqa: E402
import app.channel_tester as channel_tester  # noqa: E402
import app.config as cfgmod  # noqa: E402
import app.search_index as search_index  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO, 'app')

# What reset_module_globals() puts back, as `module path -> {name}`. Kept here rather than
# imported so the two lists have to be edited together and a silent drop on one side is a
# red test rather than a no-op.
RESET = {
    'app/account_stats.py': {'_catch_up'},
    'app/admission.py': {'_active', '_next_seq'},
    'app/auth.py': {'_failures'},
    'app/channel_search.py': {'_tag_channel_ids_cache', '_standing_breakdown_cache',
                              '_now_tag_channel_ids_cache'},
    'app/channel_tester.py': {'_state'},
    'app/concatenator.py': {'_concat_progress'},
    'app/config.py': {'_restart_needed'},
    'app/fs_utils.py': {'_last_logged_outcome'},
    'app/postprocessor.py': {'_analysis_progress'},
    'app/preview.py': {'_sessions', '_reaper'},
    'app/probe.py': {'_missing_reported'},
    'app/readiness.py': {'_ondemand', '_nav_cache'},
    'app/search_index.py': {'_rebuilding', '_stale_since'},
}

# Process-global state that is deliberately NOT reset, with who owns it instead. Every
# entry names a live resource: clearing the registry would orphan the thread, child
# process or queue it tracks, which is worse than the leak it would prevent.
ALLOWED = {
    ('app/accounts.py', '_sync_threads'): 'a running sync thread deregisters itself in its own finally',
    ('app/accounts.py', '_sync_stop_events'): 'paired with _sync_threads; clearing loses the only handle that can stop a live sync',
    ('app/accounts.py', '_sync_locks'): 'clearing would let a second sync start against an account already syncing',
    ('app/accounts.py', '_sync_progress'): 'popped by the sync thread itself (accounts.py finally block), keyed to its lifetime',
    ('app/accounts.py', '_sync_cancel_reasons'): 'read by the sync thread after cancellation; clearing loses the reason the UI reports',
    ('app/concatenator.py', '_active_concats'): 'each concat chain releases its own claim in its finally; clearing would let a second concat start against a recording already being concatenated',
    ('app/concatenator.py', '_active_join_procs'): 'live ffmpeg children, same as postprocessor._active_conversions; the join unregisters its own in its finally and kill_active_joins() is what knows how to tear one down',
    ('app/config.py', '_yaml_cache'): 'self-invalidating - keyed on the parsed path plus its stat, and ConfigSandbox drops it on both edges',
    ('app/events.py', '_subscribers'): 'each SSE generator removes its own queue on disconnect; clearing strands a live stream',
    ('app/notifications.py', '_pending'): 'drained by the flush timer, which TestApp.cleanup() cancels',
    ('app/notifications.py', '_timers'): 'live threading.Timer handles - TestApp.cleanup() cancels them, clearing would leak them',
    ('app/postprocessor.py', '_active_conversions'): 'live ffmpeg children; the conversion path is what knows how to kill them',
    ('app/postprocessor.py', '_cancel_requested'): 'read by a running conversion to decide whether to stop',
    ('app/recorder.py', '_active'): 'TestApp.cleanup() tears down what it finds here; clearing it first would strand ffmpeg',
    ('app/scheduler.py', '_app'): 'owned by init_scheduler()/shutdown; _assert_jobstore_is_sandboxed guards the escape this could cause',
    ('app/scheduler.py', '_scheduler'): 'a live BackgroundScheduler - dropping the reference leaks its threads instead of stopping them',
    ('app/toolchain.py', '_cache'): 'a fact about the machine, not about a test: the answer is identical in every module, and re-probing per module is two process spawns (~105ms each) x the module count. Keyed on the configured ffmpeg path so a changed setting re-probes anyway; a test that patches _probe_version drives describe_tools_uncached, or calls reset_cache() on both edges (tests/test_toolchain.py)',
    ('app/toolchain.py', '_cache_key'): 'paired with _cache above - clearing one without the other is what would serve a stale answer',
    ('app/toolchain.py', '_capabilities'): 'the same kind of machine fact as _cache: what the resolved ffmpeg build includes, keyed on that binary so a different one re-probes. Filled only by the tools endpoint, never at create_app(); a test that patches _run_listing drives describe_capabilities_uncached, or calls reset_cache() - which clears this too - on both edges (tests/test_toolchain.py)',
    ('app/toolchain.py', '_capabilities_key'): 'paired with _capabilities above - clearing one without the other is what would serve a stale answer',
}


def _process_globals(path):
    """Names in one module that are process-global mutable state.

    Two shapes, because they leak in two different ways. A name rebound through a `global`
    statement is state the module reassigns at run time (`_rebuilding = True`). A name bound
    to an empty dict/list/set at module level is an accumulator mutated in place, which
    needs no `global` and so the first rule cannot see it (`_active: dict = {}`).

    Populated literals are deliberately excluded: a module-level lookup table
    (`SORTS`, `ALERT_TYPES`, `_DEFAULTS`) is a constant registry, and treating those as
    leakable state would bury the real names in ~60 false positives.
    """
    with open(path, 'r', encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), path)

    def _targets(node):
        if isinstance(node, ast.Assign):
            return node.targets
        if isinstance(node, ast.AnnAssign):
            return [node.target]
        return []

    module_level = set()
    empty_containers = set()
    for node in tree.body:
        targets = _targets(node)
        for t in targets:
            if isinstance(t, ast.Name):
                module_level.add(t.id)
        value = getattr(node, 'value', None)
        if value is None or len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        is_empty = (
            (isinstance(value, ast.Dict) and not value.keys)
            or (isinstance(value, ast.List) and not value.elts)
            or (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in ('set', 'dict', 'list') and not value.args)
        )
        if is_empty:
            empty_containers.add(targets[0].id)

    rebound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            rebound.update(node.names)

    return (rebound & module_level) | empty_containers


def _app_modules():
    for root, _dirs, files in os.walk(APP_DIR):
        for name in sorted(files):
            if name.endswith('.py'):
                full = os.path.join(root, name)
                yield full, os.path.relpath(full, REPO)


class ResetRestoresPristineStateTests(unittest.TestCase):
    """Each reset global, dirtied by hand, comes back clean from make_test_app()."""

    def setUp(self):
        self.t = None

    def tearDown(self):
        if self.t is not None:
            self.t.cleanup()
        reset_module_globals()

    def test_a_mid_flight_health_check_run_does_not_survive_into_the_next_app(self):
        channel_tester._state.running = True
        channel_tester._state.current_job_id = 999
        self.t = make_test_app()
        self.assertFalse(channel_tester.is_running(),
                         'a leaked health-check run makes the next module read RUNNING jobs')
        self.assertIsNone(channel_tester._state.current_job_id)

    def test_a_leaked_index_rebuild_flag_does_not_survive_into_the_next_app(self):
        search_index._rebuilding = True
        self.t = make_test_app()
        self.assertFalse(search_index.rebuild_in_progress(),
                         "a leaked rebuild flag renders the dashboard's background "
                         "activity indicator as active")

    def test_lockout_counters_do_not_survive_into_the_next_app(self):
        auth._failures['127.0.0.1'] = ['x'] * 50
        self.t = make_test_app()
        self.assertEqual(auth._failures, {},
                         'leaked failure counts can lock the next module out of every login')

    def test_a_restart_required_latch_does_not_survive_into_the_next_app(self):
        cfgmod.set_restart_needed(True)
        self.t = make_test_app()
        self.assertFalse(cfgmod.is_restart_needed(),
                         'a leaked restart latch renders a restart banner on the next '
                         "module's pages")

    def test_a_leaked_tag_channel_id_cache_does_not_survive_into_the_next_app(self):
        """A tag id is only unique within one test's temp DB, so a stale entry from a
        previous module's tag with the same id would otherwise serve the wrong channel-id
        set here."""
        channel_search._tag_channel_ids_cache[1] = (('x',), 'stale-watermark', frozenset({1}))
        self.t = make_test_app()
        self.assertEqual(channel_search._tag_channel_ids_cache, {})

    def test_a_leaked_now_tag_cache_does_not_survive_into_the_next_app(self):
        """Same tag-id reasoning as above, and one more: this cache expires on a wall clock
        rather than on a search-index watermark, so nothing about a new temp database makes a
        leaked entry look stale. It would simply be served for the next minute."""
        channel_search._now_tag_channel_ids_cache[((1, ('x',)),)] = (
            time.monotonic() + 3600, {1: frozenset({1})})
        self.t = make_test_app()
        self.assertEqual(channel_search._now_tag_channel_ids_cache, {})

    def test_the_reset_runs_before_create_app_not_after(self):
        """Ordering matters: create_app() and anything it triggers must already see clean
        state. Resetting afterwards would clear whatever the app build itself set up."""
        channel_tester._state.running = True
        self.t = make_test_app()
        # A second app in the same process must be just as clean as the first.
        channel_tester._state.running = True
        second = make_test_app()
        try:
            self.assertFalse(channel_tester.is_running())
        finally:
            second.cleanup()


class ProcessGlobalCoverageTests(unittest.TestCase):
    """Every process-global in app/ is either reset or explicitly owned by something else.

    This is the half that keeps the fix alive. A new global added without a decision here
    is a red test now, instead of an unrelated module failing under a shard split later.
    """

    def test_every_process_global_is_reset_or_allowlisted(self):
        undecided = []
        for full, rel in _app_modules():
            for name in sorted(_process_globals(full)):
                if name in RESET.get(rel, set()):
                    continue
                if (rel, name) in ALLOWED:
                    continue
                undecided.append(f'{rel}::{name}')
        self.assertEqual(
            undecided, [],
            'process-global mutable state in app/ with no isolation decision: '
            f'{undecided}. Either reset it in tests/support/app.py::reset_module_globals '
            '(add it to RESET here in the same edit), or add an ALLOWED entry naming what '
            'already owns its cleanup. Leaving it undecided means a future shard split '
            'fails in an unrelated module.')

    def test_the_reset_list_still_matches_the_source(self):
        """RESET may not name something that is no longer a global - a stale entry reads as
        coverage this test is not actually providing."""
        stale = []
        for rel, names in RESET.items():
            actual = _process_globals(os.path.join(REPO, rel))
            stale.extend(f'{rel}::{n}' for n in sorted(names - actual))
        self.assertEqual(stale, [], f'RESET names globals that no longer exist: {stale}')

    def test_the_allowlist_still_matches_the_source(self):
        stale = []
        for (rel, name) in sorted(ALLOWED):
            if name not in _process_globals(os.path.join(REPO, rel)):
                stale.append(f'{rel}::{name}')
        self.assertEqual(stale, [], f'ALLOWED names globals that no longer exist: {stale}')

    def test_the_scan_finds_the_globals_that_actually_caused_the_failures(self):
        """A scan that quietly stopped matching anything would pass every assertion above
        while guarding nothing, so pin the two names that produced the real failures."""
        self.assertIn('_state', _process_globals(os.path.join(REPO, 'app/channel_tester.py')))
        self.assertIn('_rebuilding', _process_globals(os.path.join(REPO, 'app/search_index.py')))


class TesterResetIdiomTests(unittest.TestCase):
    """`_reset_run_state()` is a START-of-run function, so it may never be used as cleanup.

    It takes the KIND_TESTER admission ticket in the same critical section that sets
    is_running (dev/changelog/679), and only `_end_run()` gives that ticket back. A module
    that calls it from setUp/tearDown therefore leaks a ticket per test, and the next module
    that asks admission for a kind the tester blocks - without building a TestApp first, so
    `reset_module_globals()` never runs for it - is refused by a run that never happened.
    That is not hypothetical: it made `test_admission` red by shard order alone
    (dev/changelog/723), and three more modules still carried the same idiom afterwards.

    Calling it inside a test BODY is correct and stays allowed - simulating a running tester
    is exactly what it is for.
    """

    FIXTURES = {'setUp', 'tearDown', 'setUpClass', 'tearDownClass', 'setUpModule',
                'tearDownModule'}

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _fixture_calls(self, path):
        with open(path) as f:
            source = f.read()
        # Substring test before the parse: this scan walks every file under tests/, and
        # parsing ~200 of them to find the handful that mention the name at all is the
        # whole cost of the check.
        if '_reset_run_state' not in source:
            return []
        tree = ast.parse(source, path)
        found = []
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name not in self.FIXTURES:
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == '_reset_run_state'):
                        found.append(f'{cls.name}.{fn.name}')
        return found

    def test_no_test_fixture_uses_reset_run_state_as_cleanup(self):
        offenders = []
        tests_dir = os.path.join(REPO, 'tests')
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith('.py'):
                continue
            for where in self._fixture_calls(os.path.join(tests_dir, name)):
                offenders.append(f'tests/{name}::{where}')
        self.assertEqual(
            offenders, [],
            'channel_tester._reset_run_state() called from a test fixture: '
            f'{offenders}. It acquires the KIND_TESTER admission ticket, so using it as '
            'setUp/tearDown cleanup leaks one per test. Drop it from setUp '
            '(make_test_app() already installs a fresh RunState) and call '
            'channel_tester._end_run() in tearDown instead.')

    def test_the_scan_would_catch_the_idiom_it_guards(self):
        """A scan that matched nothing would pass the assertion above while guarding
        nothing, so run it against the exact shape that was really in the tree."""
        source = (
            'class FooTests(unittest.TestCase):\n'
            '    def tearDown(self):\n'
            '        with channel_tester._lock:\n'
            '            channel_tester._reset_run_state()\n'
            '    def test_body_calls_are_fine(self):\n'
            '        channel_tester._reset_run_state()\n'
        )
        path = os.path.join(self._tmpdir, 'sample.py')
        with open(path, 'w') as f:
            f.write(source)
        self.assertEqual(self._fixture_calls(path), ['FooTests.tearDown'])


if __name__ == '__main__':
    unittest.main()
