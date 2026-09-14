"""Regression-suite timing harness and runner: a test for the tests.

Runs the same unittest discovery `run_tests.sh` used to run directly, but times it,
appends one record per run to `tests/timing_history.jsonl` (committed), and prints a
single grep-able status line comparing this run against a rolling baseline plus a hard
ceiling. Timing NEVER changes the exit code - the unittest pass/fail result governs it
(non-fatal by design, decided 2026-07-21). Invoked as `python3 -m tests.support.timing`
from run_tests.sh; importing under the `tests` package installs netguard/notifyguard first
(tests/__init__.py), so the same isolation guarantees apply as a direct discover run.

The status line always prints, both to make a regression impossible to miss and so the
agent running the suite can relay it every time (CLAUDE.md ## Testing).

## Sharding (added 2026-08-11, dev/changelog/583)

By default the suite is split across `DEFAULT_JOBS` worker processes and takes ~218s
instead of ~386s. `-j 1` restores the original single-process path exactly and is the
escape hatch for debugging anything order-dependent.

Sharding is here rather than in a second runner because this module already owns the run,
the record and the status line, and two runners would drift. It is process-based, not
thread-based, for two independent reasons: the tests patch module-level state
(`app.config._CONFIG_PATH`, `recorder._active`) that is process-global, and roughly a third
of the suite's wall clock is a single process *waiting* - 59s in `time.sleep` and 74s on
379 child processes - so the win comes from overlapping idle time, not from cores. That is
also why 3 shards beat 2 on a 2-core box.

Shards are built from whole **modules**, never individual tests, so a class keeps its
`setUpClass` in one worker; they are packed by measured cost from the last passing history
record, longest first. Full measurement record and method: `dev/docs/PERF-test-suite.md`.

## Noticing drift, not only spikes (added 2026-08-21, dev/changelog/777)

A rolling median rejects noise well and absorbs a step change completely - the slower run
joins the window the next run is judged against - and the hard ceiling that was supposed to
backstop that has been re-baselined six times, twice with no measurement behind it. So the
line also reports **milliseconds per test** on every run and warns when that figure has
stepped up and stayed up. ms/test is the size-independent half of the story: the wall clock
grows when tests are added, which is not a regression, and every re-baseline note in the
wall-clock era said per-test cost was the number to watch. Since 2026-08-27 the ceiling reads
it too (`CEILINGS_MS_PER_TEST`), so the wall clock is now watched only by the rolling median,
which moves with the suite instead of having to be re-baselined by hand.

## Naming the contention (added 2026-08-27, dev/changelog/835)

Every run also records how much CPU the box spent on work that was NOT this suite, and the
status line says so. This box has two cores and no swap, and a run sharing it with a browser
or a build measures ~30s slower than the same tree measured alone - which for a long time was
the single largest term in the `[SUITE-TIMING]` number and the only one a reader could not
see. It is disclosure, not yet a gate: no recorded run carries the figure, so a comparability
threshold cannot be chosen by replay the way every other threshold here was, and inventing one
is what the ceiling redesign exists to stop.

## Keeping a red run diagnosable (added 2026-08-17, dev/changelog/717)

A run that fails leaves two artifacts behind, because otherwise it leaves none: the failing
test ids go into the history record next to the counts, and every worker's verbose output is
captured under `dev/test-logs/<timestamp>/`. Before this, a shard that reported FAILED and a
rerun that came back green destroyed the identity of the failure permanently, so the only
move left was to re-run the suite and guess. Both artifacts are strictly best-effort - a log
file that cannot be opened or written degrades to no capture and never changes the run's
result, the same way timing itself is non-fatal.
"""
import argparse
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone

# tests/support/timing.py -> repo root is two dirs up from support/
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
HISTORY_PATH = os.path.join(_REPO, 'tests', 'timing_history.jsonl')

# Rolling-baseline tolerance: flag a run that exceeds the recent median by more than this.
TOL = 0.30
# Minimum passing same-tier runs before a baseline comparison is meaningful.
MIN_BASELINE_RUNS = 3
# How many recent passing same-tier runs feed the rolling median.
BASELINE_WINDOW = 10
# Absolute hard ceiling, in MILLISECONDS PER TEST - the backstop against slow creep the
# rolling median would otherwise "boil-frog" past. The unit is in the name because it used to
# be wall-clock seconds, and a wall clock cannot express the thing this is watching: it moves
# when the suite GROWS (2,756 -> 4,630 tests in 15 days, no slowdown involved) and when
# something ELSE is using the box, so it fired on 8 of the last 30 green runs and had been
# re-baselined seven times, the last two with no measurement behind them. ms/test is
# size-independent - it read 80.8ms across the first 30 recorded green runs and 85.7ms across
# the last 30, through that same 68% growth - and is the figure every one of those seven
# re-baseline notes already named as the one to watch.
#
# Derivation is the same one every prior ceiling used, measured level plus ~30% headroom.
#   -j 3: 85.7ms/test, the median of the last 30 green records, x 1.30. Replayed over all 227
#     green -j 3 records, 110 fires on 1 of them - against 16 for the 400s wall ceiling it
#     replaces, and 8 of the last 30.
#   -j 1: 161.1ms/test, measured fresh (747.8s / 4,643 tests, green), because the 300-record
#     history cap had aged out every serial record and there was nothing to replay against.
#     That measurement itself carried 10% foreign CPU, so it reads a little high and the
#     ceiling derived from it is correspondingly loose - the right direction for the
#     diagnostic path, where a WARNING nobody is acting on costs more than it earns.
# Full reasoning, the replay table, and the history of the wall-clock era:
# `dev/docs/PERF-test-suite.md` §6 - that file is the record, not this comment.
CEILINGS_MS_PER_TEST = {('0-2', 1): 210.0, ('0-2', 3): 110.0}

# ── Per-test drift (added 2026-08-21, dev/changelog/777) ─────────────────────
# Neither check above can see a permanent step that lands under TOL: the slower run joins the
# window the next run compares against, so the rolling median walks up to meet it, and the
# ceiling - the thing that was supposed to backstop exactly that - had been raised seven times,
# the last two without a fresh measurement behind them. Between them the two devices agree to
# absorb any regression that arrives in small enough pieces.
#
# The figure this check reads is the one every one of those re-baselines already named as the
# number to watch, and nothing computed it: **milliseconds per test**. It is derived, never
# stored (total_wall / test_count), and it is the right statistic here because it is
# size-independent - the suite's wall clock grows when tests are ADDED, which is not a
# regression, while ms/test does not.
#
# Same sustained-shift shape as dev/tools/search_timing_check.py, and segmented by the same
# _passing_same_tier the median already uses, because ms/test is no more comparable across a
# -j change than total_wall is. Replayed over the 300 committed records these thresholds fire
# 4 times, all of them multi-run elevated sessions; the current suite (~85ms/test at -j 3) is
# flat, which is the answer to "has the suite been quietly drifting" - measured continuously
# now instead of by hand at each re-baseline.
#
# Re-verified 2026-08-27 (dev/changelog/835) rather than re-tuned. The +11% step that was
# filed as a drift has receded on its own: ms/test reads 80.8 as a median over the first 30
# recorded green runs and 85.7 over the last 30, at 68% more tests. So the arm that had been
# firing alongside the ceiling was the CEILING, and these thresholds are left exactly as they
# were - a check that fires on 4 of 227 green runs is calibrated, not noisy.
PER_TEST_DRIFT_TOL = 0.10
PER_TEST_DRIFT_FLOOR_MS = 5.0
DRIFT_WINDOW = 20
DRIFT_RECENT = 3
DRIFT_MIN_PRIOR = 5
# Keep the committed history from growing without bound.
MAX_HISTORY_LINES = 300
# Captured worker output, one directory per run. Local disk, gitignored.
LOG_ROOT = os.path.join(_REPO, 'dev', 'test-logs')
# How many runs' logs to keep. A red run whose rerun is green must still have its own
# directory afterwards, which is the whole point - so this has to be more than one.
MAX_LOG_RUNS = 10
# Ceiling on the ids stored per record. A catastrophic run can fail thousands of tests and
# tests/timing_history.jsonl is committed; `failures` + `errors` still carry the true totals,
# so a stored list shorter than their sum means it was truncated here.
MAX_FAILED_IDS = 50
# Shards for a default run. 3 beats 2 (205.7s vs ~220s) on this 2-core box because a third
# of the suite is idle waiting on sleeps and child processes; see the module docstring.
DEFAULT_JOBS = 3

STATUS_PREFIX = '[SUITE-TIMING]'


class _TimingResult(unittest.TextTestResult):
    """TextTestResult that records per-test wall time (keeps the -v per-test output).

    Also records FIXTURE time - everything that happens between one test stopping and the
    next one starting, which is setUpClass/tearDownClass/setUpModule. That is invisible to
    startTest..stopTest, and the blind spot was not academic: on 2026-08-05
    tests/test_channel_search_page_js.py was the single most expensive module in the suite
    at 34.6s (154 tests, ALL of their work done in one setUpClass) and had never once
    appeared in a `[SUITE-TIMING]` movers list, because per_module reported it as 0.0s.
    The gap is charged to the class that was about to run, which is the one whose
    setUpClass just paid for it.
    """
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.timings = []          # list[(seconds, test_id)]
        self.fixtures = {}         # {ClassName: seconds spent in its setUpClass}
        self._last_stop = None

    def startTestRun(self):
        # Anchor here, not on the first stopTest: discovery has already finished by the
        # time the runner calls this, so the gap to the FIRST test is that test's own
        # setUpClass. Leaving it unanchored is not a rounding error - the alphabetically
        # first module is test_channel_search_page_js, whose whole 26s fixture is paid
        # before its first test and would be the one bucket the fix still missed.
        super().startTestRun()
        self._last_stop = time.perf_counter()

    def startTest(self, test):
        now = time.perf_counter()
        if self._last_stop is not None:
            key = _class_key(test.id())
            self.fixtures[key] = self.fixtures.get(key, 0.0) + (now - self._last_stop)
        self._t0 = now
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        now = time.perf_counter()
        self.timings.append((now - self._t0, test.id()))
        self._last_stop = now


def _failed_ids(result):
    """Ids of the tests that failed or errored, deduplicated and in a stable order.

    A `setUpClass`/`setUpModule` error arrives as a `_ErrorHolder` whose `id()` reads
    `setUpClass (tests.test_x.FooTests)`; that is kept as-is, because naming the fixture is
    exactly as useful as naming a test method.
    """
    return sorted({t.id() for t, _ in result.failures} | {t.id() for t, _ in result.errors})


# A failing subTest's id is the test id plus whatever the assertion was parametrized on:
# `tests.test_x.FooTests.test_y (file='dev/tasks/...', dep="...")`. Group 1 requires a dot,
# which is what separates a dotted test id from an `_ErrorHolder`'s `setUpClass (tests.x.Y)`
# - there the parenthesis names the class and is the useful half, so it must survive.
_SUBTEST_ID = re.compile(r'^([\w.]+\.[\w.]+) \(.*\)$', re.DOTALL)


def _history_safe_ids(failed_ids):
    """Failed-test ids with any subTest parameter suffix dropped, deduplicated in order.

    tests/timing_history.jsonl is committed AND published, so every byte written here is
    published text. A subTest suffix is arbitrary - it carries whatever the test happened to
    be parametrized on - and twice now that has been a path under `dev/tasks/` and a personal
    name, which the leak scanner then refuses (`dev/changelog/881`, and again on 2026-09-13).
    The bare id still answers the question the field exists for: which test failed.
    """
    seen = []
    for tid in failed_ids:
        m = _SUBTEST_ID.match(tid)
        safe = m.group(1) if m else tid
        if safe not in seen:
            seen.append(safe)
    return seen


def _prepare_log_dir():
    """Make this run's capture directory and prune old ones. None if it cannot be made.

    Capture is best-effort by construction: a run must never fail, change its exit code, or
    lose its status line because a log file could not be created.
    """
    try:
        os.makedirs(LOG_ROOT, exist_ok=True)
        existing = sorted(
            d for d in os.listdir(LOG_ROOT)
            if os.path.isdir(os.path.join(LOG_ROOT, d)))
        for old in existing[:max(0, len(existing) - (MAX_LOG_RUNS - 1))]:
            shutil.rmtree(os.path.join(LOG_ROOT, old), ignore_errors=True)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        path = os.path.join(LOG_ROOT, stamp)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(LOG_ROOT, f'{stamp}-{suffix}')
        os.makedirs(path)
        return path
    except OSError:
        return None


def _open_log(log_dir, name):
    """A capture file under `log_dir`, or None when capture is off or unavailable."""
    if not log_dir:
        return None
    try:
        return open(os.path.join(log_dir, name), 'w', encoding='utf-8')
    except OSError as e:
        print(f'{STATUS_PREFIX} could not capture output to {name}: {e}',
              file=sys.stderr, flush=True)
        return None


class _Tee:
    """Write-through to a live stream and a capture file at once.

    Used on the `-j 1` path, which has no worker pipe for `_pump` to intercept. Only the
    handful of methods `unittest.TextTestRunner` reaches for through its `_WritelnDecorator`
    are implemented, and a write to the capture file can never propagate.
    """

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        written = self._stream.write(s)
        try:
            self._fh.write(s)
        except (OSError, ValueError):
            pass  # best-effort capture: a full disk must not fail the run
        return written

    def flush(self):
        self._stream.flush()
        try:
            self._fh.flush()
        except (OSError, ValueError):
            pass  # see write()


def _cpu_snapshot():
    """`(whole-box busy CPU seconds, this process tree's CPU seconds)`, or None.

    The two are read together because only their difference means anything: subtracting our
    own consumption from the box's leaves the CPU that went to something else, which is the
    one term in a run's wall clock that nothing has ever recorded.

    `RUSAGE_CHILDREN` accumulates a descendant's CPU into ours as each level reaps it, so a
    worker's ffmpeg and node children are counted as ours rather than as foreign - provided
    they were waited for. A leaked child is not, and reading it as foreign load is the honest
    answer anyway.

    Best-effort, exactly like the log capture: a box with no `/proc/stat` (or a `sysconf`
    that will not answer) returns None and the run simply records no contention figure.
    """
    try:
        with open('/proc/stat', 'r') as fh:
            fields = [int(x) for x in fh.readline().split()[1:]]
        hz = os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None
    if len(fields) < 5 or not hz:
        return None
    idle = fields[3] + fields[4]          # idle + iowait
    own = 0.0
    for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN):
        ru = resource.getrusage(who)
        own += ru.ru_utime + ru.ru_stime
    return (sum(fields) - idle) / hz, own


def _foreign_cpu(before, after, wall):
    """`(seconds, share of the box)` that went to other work, or None when unmeasured.

    The share is against `wall * cpu_count` - the core-seconds the box had to give - so it
    reads as "this fraction of the machine was somebody else's" rather than as a load average
    nobody can interpret. Clamped at both ends: sampling is not atomic with the run, and a
    negative or above-1 figure would be an artifact rather than a measurement.
    """
    if before is None or after is None or not wall or wall <= 0:
        return None
    seconds = max(0.0, (after[0] - before[0]) - (after[1] - before[1]))
    cores = os.cpu_count() or 1
    return seconds, min(1.0, seconds / (wall * cores))


def _git_info():
    def _run(args):
        try:
            return subprocess.run(
                ['git'] + args, cwd=_REPO, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ''
    commit = _run(['rev-parse', '--short', 'HEAD']) or 'unknown'
    dirty = bool(_run(['status', '--porcelain']))
    return commit, dirty


def _class_key(test_id):
    """The per-class bucket a test id belongs to.

    test.id() looks like 'test_pkg.test_mod.ClassName.test_method'; the class name is a
    stable, human-meaningful bucket for "which area got slower".
    """
    parts = test_id.split('.')
    return parts[-2] if len(parts) >= 2 else test_id


def _per_module(timings):
    """Aggregate per-test timings into {ClassName: seconds}."""
    agg = {}
    for dur, tid in timings:
        key = _class_key(tid)
        agg[key] = agg.get(key, 0.0) + dur
    return agg


def _charged(record):
    """Total time charged to each class: its tests plus its own setUpClass.

    `per_module` keeps meaning exactly what it has always meant (in-test time) so the
    committed history stays comparable across the re-baselines; this is the figure the
    movers list should actually rank on. Records written before 2026-08-05 carry no
    fixture key and simply read as zero.
    """
    agg = dict(record.get('per_module', {}))
    for k, v in record.get('per_module_fixture', {}).items():
        agg[k] = agg.get(k, 0.0) + v
    return agg


def _load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    records = []
    with open(HISTORY_PATH, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # tolerate a partially-written trailing line; skip it
    return records


def _append_history(record):
    records = _load_history()
    records.append(record)
    if len(records) > MAX_HISTORY_LINES:
        records = records[-MAX_HISTORY_LINES:]
    tmp = HISTORY_PATH + '.tmp'
    with open(tmp, 'w') as f:
        for r in records:
            f.write(json.dumps(r, sort_keys=True) + '\n')
    os.replace(tmp, HISTORY_PATH)


def _median(values):
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _jobs_of(record):
    """How many worker processes a record was produced by.

    Records written before sharding existed carry no `jobs` key and were all serial, so
    they read as 1 - which keeps every one of them comparable against a `-j 1` run today.
    """
    return record.get('jobs', 1)


def _passing_same_tier(records, tier, jobs, shard_order='forward'):
    """Prior passing runs comparable with this one.

    Filtered on `jobs` as well as `tier` for the same reason `tier` was there first: a
    3-shard wall clock and a serial wall clock measure different things, and a median
    mixing them would describe neither.

    `shard_order` segments it once more. `--reverse-shards` packs lightest-first on purpose,
    which produces a deliberately worse-balanced split and therefore a slower run (232.6s
    against 217.9s forward, measured). It is a correctness check, not a performance
    measurement, so its records must not drag the forward baseline up and mask a real
    regression. Records predating this key read as forward, which is what they were.
    """
    return [
        r for r in records
        if r.get('tier') == tier and _jobs_of(r) == jobs
        and r.get('shard_order', 'forward') == shard_order
        and r.get('failures', 0) == 0 and r.get('errors', 0) == 0
    ]


def _ms_per_test(record):
    """Milliseconds of wall clock per test, or None when the record cannot answer.

    Derived rather than stored, so every one of the 300 committed records already carries it
    and no migration is needed. A record with no `test_count` (or a run that collected
    nothing) has no per-test cost at all - it must read as absent rather than as zero, or a
    missing figure would look like the fastest suite ever recorded.
    """
    count = record.get('test_count') or 0
    wall = record.get('total_wall')
    if not count or not isinstance(wall, (int, float)):
        return None
    return float(wall) / count * 1000.0


def _per_test_drift(prior_passing, record):
    """`(before, recent)` ms/test when per-test cost has stepped up and stayed, else None.

    The last DRIFT_RECENT runs (this one included) against the earlier ones in the window,
    both sides as medians so one contended run cannot invent a drift, and the current run has
    to still be over the earlier level - a suite that has already come back down is not
    drifting now. `prior_passing` is already tier/jobs/shard_order filtered by the caller.
    """
    now = _ms_per_test(record)
    if now is None:
        return None
    series = [v for v in (_ms_per_test(r) for r in prior_passing) if v is not None]
    window = (series + [now])[-DRIFT_WINDOW:]
    if len(window) < DRIFT_RECENT + DRIFT_MIN_PRIOR:
        return None
    recent = _median(window[-DRIFT_RECENT:])
    before = _median(window[:-DRIFT_RECENT])

    def _over(value):
        return (before > 0 and value > before * (1 + PER_TEST_DRIFT_TOL)
                and (value - before) > PER_TEST_DRIFT_FLOOR_MS)

    return (before, recent) if _over(recent) and _over(now) else None


def _top_movers(this_pm, base_pm, n=5):
    deltas = []
    for k in set(this_pm) | set(base_pm):
        d = this_pm.get(k, 0.0) - base_pm.get(k, 0.0)
        if d > 0.05:
            deltas.append((d, k))
    deltas.sort(reverse=True)
    return deltas[:n]


def _status_line(record, prior_passing):
    """Build the always-printed INFO/WARNING status line for this run."""
    tier = record['tier']
    total = record['total_wall']
    count = record['test_count']
    jobs = _jobs_of(record)
    ceiling = CEILINGS_MS_PER_TEST.get((tier, jobs))

    # Sum of per-test plus setUpClass time. Read it as "how much test-time this run
    # accounted for", NOT as a machine-independent constant: it is per-test *wall* time, so
    # it inflates under contention - 620.2s across 3 shards against 388.7s for the same
    # tests serially, a 1.6x stretch from three workers sharing 2 cores. Useful within a
    # fixed -j (it is what the movers list ranks on) and useful as a contention read; not
    # comparable across a -j change. Only compare it against a run with the same `jobs`,
    # which is exactly what _passing_same_tier already enforces.
    charged = record.get('total_charged')
    jobs_txt = f", {jobs} shards, {charged:.1f}s charged" if jobs > 1 and charged else ''

    # Named on every line, INFO included: wall clock grows when tests are added, so ms/test is
    # the figure that says whether the suite got SLOWER rather than bigger, and six ceiling
    # re-baselines in a row have said so in prose without anything printing the number.
    # The foreign-CPU share rides in the same parenthesis and for the same reason: it is the
    # other half of why a wall clock moves, and until it was measured a reader had no way to
    # tell a contended run from a slow one.
    per_test = _ms_per_test(record)
    share = record.get('foreign_cpu_share')
    facts = ([f'{per_test:.1f}ms/test'] if per_test is not None else [])
    if share is not None:
        facts.append(f'{share * 100:.0f}% foreign CPU')
    per_test_txt = f" ({', '.join(facts)})" if facts else ''

    window = prior_passing[-BASELINE_WINDOW:]
    if len(window) < MIN_BASELINE_RUNS:
        return (f"{STATUS_PREFIX} INFO {total:.1f}s / {count} tests{per_test_txt} - baseline "
                f"warming up ({len(window)}/{MIN_BASELINE_RUNS} passing runs recorded at "
                f"-j {jobs}){jobs_txt}")

    baseline = _median([r['total_wall'] for r in window])
    over_baseline = total > baseline * (1 + TOL)
    # Against ms/test, never the wall clock: a run cannot be over the ceiling merely for
    # having more tests in it. A run that collected nothing has no per-test cost and so
    # cannot be judged here - the shard-partition and missing-emit checks own that failure.
    over_ceiling = ceiling is not None and per_test is not None and per_test > ceiling
    # Deliberately reads the WHOLE passing same-tier history, not `window`: a step change is
    # only visible while the window still remembers both sides of it, and the rolling median
    # is capped at 10 runs precisely so it forgets quickly.
    drift = _per_test_drift(prior_passing, record)

    # Reported on every line, INFO included: fixture time is the half of the suite that
    # per_module cannot see, and a 34.6s module hid inside it for a week (_TimingResult).
    fixture = sum(record.get('per_module_fixture', {}).values())
    fixture_txt = f", {fixture:.1f}s in setUpClass" if fixture >= 1.0 else ''

    if not (over_baseline or over_ceiling or drift):
        return (f"{STATUS_PREFIX} INFO {total:.1f}s / {count} tests{per_test_txt} OK - "
                f"within baseline (median {baseline:.1f}s over last {len(window)}, ceiling "
                f"{'n/a' if ceiling is None else f'{ceiling:.0f}ms/test'}"
                f"{fixture_txt}{jobs_txt})")

    reasons = []
    if over_baseline:
        pct = (total / baseline - 1) * 100
        reasons.append(f"+{pct:.0f}% vs baseline {baseline:.1f}s")
    if over_ceiling:
        reasons.append(f"over ceiling {ceiling:.0f}ms/test")
    if drift:
        before, recent = drift
        reasons.append(f"per-test cost drifting {before:.1f} -> {recent:.1f}ms/test "
                       f"(+{(recent / before - 1) * 100:.0f}%) over the last "
                       f"{DRIFT_RECENT} runs")
    if fixture_txt:
        reasons.append(fixture_txt.lstrip(', '))
    if jobs_txt:
        reasons.append(jobs_txt.lstrip(', '))
    movers = _top_movers(_charged(record), _charged(window[-1]))
    movers_txt = ('; top movers: ' + ', '.join(f"{k} +{d:.1f}s" for d, k in movers)) if movers else ''
    return (f"{STATUS_PREFIX} WARNING {total:.1f}s / {count} tests{per_test_txt} - "
            f"{', '.join(reasons)}{movers_txt}")


# ── Sharding ──────────────────────────────────────────────────────────────────

def _discover_modules():
    """Every test module, as dotted names. Sorted, so a shard split is reproducible.

    A flat listing of `tests/test_*.py` rather than a `loader.discover()` walk, because the
    parent must not import the suite: importing it in the parent AND in every worker would
    pay the whole import cost N+1 times. There are no test modules below `tests/` itself
    (`tests/support` and `tests/fixtures` hold no `test_*.py`), so the two agree, and
    _assert_no_module_was_dropped is what proves they still do.
    """
    tests_dir = os.path.join(_REPO, 'tests')
    return sorted(
        'tests.' + f[:-3] for f in os.listdir(tests_dir)
        if f.startswith('test_') and f.endswith('.py')
    )


def _module_weights(modules, history):
    """Estimated seconds per module, from the most recent passing run.

    History is per *class*, not per module, so each module is read once to find out which
    classes it declares. That is a cheap text scan, not an import. A module with no history
    (brand new, or renamed) gets the mean, which is a better guess than zero - zero would
    pile every new module into one shard.

    These are relative weights, not predictions. A record from a sharded run carries per-test
    times inflated by contention (~1.6x on this box), so the absolute planned-load figures
    printed at the start of a run read high after the first parallel record lands. Packing
    only ever compares weights against each other, so that scales out.
    """
    charged = {}
    for record in reversed(history):
        if record.get('failures', 0) == 0 and record.get('errors', 0) == 0:
            charged = _charged(record)
            break
    if not charged:
        return {m: 1.0 for m in modules}

    weights = {}
    for mod in modules:
        path = os.path.join(_REPO, mod.replace('.', os.sep) + '.py')
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                classes = re.findall(r'^class (\w+)', fh.read(), re.M)
        except OSError:
            classes = []
        weights[mod] = sum(charged.get(c, 0.0) for c in classes)

    known = [w for w in weights.values() if w > 0]
    fallback = (sum(known) / len(known)) if known else 1.0
    return {m: (w if w > 0 else fallback) for m, w in weights.items()}


def _pack_shards(modules, jobs, weights, reverse=False):
    """Greedy longest-processing-time bin packing: heaviest module into the lightest shard.

    Whole modules only, never individual tests, so a class cannot be separated from the
    `setUpClass` that pays for it. LPT is the right algorithm here because the distribution
    is long-tailed - one 25s module and a hundred sub-second ones - and it measured 139/139/139
    against a 205.7s run, i.e. balanced enough that a smarter packer would buy nothing.

    `reverse` packs lightest-first instead. It produces a *different but still valid* split
    and exists so the shuffle check in dev/docs/PERF-test-suite.md can prove the suite is
    order-independent rather than merely surviving one particular arrangement.
    """
    shards = [[] for _ in range(jobs)]
    load = [0.0] * jobs
    ordered = sorted(modules, key=lambda m: (weights.get(m, 0.0), m), reverse=not reverse)
    for mod in ordered:
        i = load.index(min(load))
        shards[i].append(mod)
        load[i] += weights.get(mod, 0.0)
    # Drop shards that got nothing (only possible with -j above the module count) and their
    # loads together, so the printed plan always has one entry per worker actually spawned.
    kept = [(s, load[i]) for i, s in enumerate(shards) if s]
    return [s for s, _ in kept], [w for _, w in kept]


def _assert_no_module_was_dropped(modules, shards):
    """A module that lands in no shard is a silently smaller suite that still prints OK.

    That is the exact false-assurance failure that got the `--all` flag removed
    (dev/changelog/513), so it is a hard error rather than a warning.
    """
    assigned = [m for s in shards for m in s]
    missing = sorted(set(modules) - set(assigned))
    dupes = sorted({m for m in assigned if assigned.count(m) > 1})
    if missing or dupes:
        raise SystemExit(
            f'{STATUS_PREFIX} FATAL shard split is not a partition of the suite - '
            f'missing={missing} duplicated={dupes}')


def _pump(stream, tag, lock, log_fh=None):
    """Relay one worker's verbose output live, tagged with its shard number.

    Line-buffered on purpose. unittest's verbosity=2 writes a test's description and its
    result in two separate calls, so reading by line reassembles `... ok` onto the line it
    belongs to; reading by chunk would shred three workers' output into each other.

    `log_fh` gets the same lines untagged, which is what survives the run for a later reader.
    """
    try:
        for line in stream:
            if log_fh is not None:
                try:
                    log_fh.write(line)
                except (OSError, ValueError):
                    pass  # best-effort capture: a full disk must not fail the run
            with lock:
                sys.stderr.write(f'[{tag}] {line}')
    finally:
        if log_fh is not None:
            log_fh.close()
        stream.close()


def _run_worker(modules, emit_path):
    """The worker half: run this shard's modules and write the result where the parent reads it."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromNames(modules))
    runner = unittest.TextTestRunner(
        resultclass=_TimingResult, verbosity=2, stream=sys.stderr,
    )
    t0 = time.perf_counter()
    result = runner.run(suite)
    payload = {
        'wall': time.perf_counter() - t0,
        'test_count': result.testsRun,
        'failures': len(result.failures),
        'errors': len(result.errors),
        'per_module': _per_module(result.timings),
        'fixtures': {k: v for k, v in result.fixtures.items() if v >= 0.05},
        'failed_ids': _failed_ids(result),
        'successful': result.wasSuccessful(),
    }
    with open(emit_path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh)
    # The worker's own exit code is advisory - the parent decides from the merged payload -
    # but keep it honest so a shard can be re-run by hand and behave normally.
    return 0 if result.wasSuccessful() else 1


def _run_sharded(shards, log_dir=None):
    """Spawn one worker per shard, relay their output, and merge their results.

    Returns a dict shaped like the fields `main()` needs, plus `successful`.
    """
    tmpdir = tempfile.mkdtemp(prefix='dvr_suite_shards_')
    procs, emits, pumps = [], [], []
    lock = threading.Lock()
    t0 = time.perf_counter()
    for i, shard in enumerate(shards):
        emit = os.path.join(tmpdir, f'shard{i}.json')
        emits.append(emit)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'tests.support.timing', '--worker', '--emit', emit] + shard,
            cwd=_REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        procs.append(proc)
        t = threading.Thread(target=_pump,
                             args=(proc.stdout, i, lock, _open_log(log_dir, f'shard{i}.log')),
                             daemon=True)
        t.start()
        pumps.append(t)

    for proc, t in zip(procs, pumps):
        proc.wait()
        t.join(timeout=30)
    total_wall = time.perf_counter() - t0

    merged = {'test_count': 0, 'failures': 0, 'errors': 0,
              'per_module': {}, 'fixtures': {}, 'failed_ids': [], 'successful': True,
              'total_wall': total_wall}
    for i, (proc, emit) in enumerate(zip(procs, emits)):
        if not os.path.exists(emit):
            # A worker that died without reporting means an unknown number of tests never
            # ran. Never let that read as a pass.
            raise SystemExit(
                f'{STATUS_PREFIX} FATAL shard {i} exited {proc.returncode} without writing '
                f'a result - {len(shards[i])} modules did not report. Re-run with -j 1.')
        with open(emit, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        merged['test_count'] += payload['test_count']
        merged['failures'] += payload['failures']
        merged['errors'] += payload['errors']
        merged['successful'] = merged['successful'] and payload['successful']
        # A worker written before this key existed cannot occur (parent and worker are one
        # file), but a hand-run older worker could, so read it defensively.
        merged['failed_ids'].extend(payload.get('failed_ids', []))
        for key in ('per_module', 'fixtures'):
            for k, v in payload[key].items():
                merged[key][k] = merged[key].get(k, 0.0) + v
        print(f'{STATUS_PREFIX} shard {i}: {payload["test_count"]} tests, '
              f'{payload["wall"]:.1f}s, {"OK" if payload["successful"] else "FAILED"}',
              file=sys.stderr, flush=True)

    merged['failed_ids'] = sorted(set(merged['failed_ids']))
    shutil.rmtree(tmpdir, ignore_errors=True)
    return merged


def _parse_args(argv):
    p = argparse.ArgumentParser(prog='run_tests.sh', add_help=True)
    p.add_argument('-j', '--jobs', type=int,
                   default=int(os.environ.get('TEST_JOBS', DEFAULT_JOBS)),
                   help=f'worker processes to split the suite across (default {DEFAULT_JOBS}; '
                        '1 runs everything in one process, the original path)')
    p.add_argument('--reverse-shards', action='store_true',
                   help='pack shards lightest-first - a different valid split, for proving '
                        'the suite is order-independent')
    # Internal: how the parent invokes a worker. Not for humans.
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--emit', help=argparse.SUPPRESS)
    p.add_argument('modules', nargs='*', help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None):
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.worker:
        sys.exit(_run_worker(args.modules, args.emit))
    if args.modules:
        # This runner is whole-suite only: it exists to produce one comparable history
        # record. Silently ignoring a named module would run the entire suite while the
        # caller believed they had asked for one file - so say so instead.
        raise SystemExit(
            f'{STATUS_PREFIX} FATAL this runner takes no module arguments '
            f'(got {args.modules}). For one module: python3 -m unittest ' + args.modules[0])

    tier = '0-2'
    jobs = max(1, args.jobs)
    shard_order = 'reverse' if args.reverse_shards else 'forward'

    log_dir = _prepare_log_dir()

    # Brackets discovery and shard planning as well as the run itself. Both of those are our
    # own CPU, which cancels out of the busy-minus-own subtraction, so the figure stays a
    # measure of OTHER work - it is only the denominator (total_wall) that is a shade short.
    cpu_before = _cpu_snapshot()

    if jobs == 1:
        # The original single-process path: one discover, one runner, no worker pipe. The
        # only addition is the capture tee, since there is no `_pump` here to intercept.
        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(_REPO, 'tests'), pattern='test_*.py')
        log_fh = _open_log(log_dir, 'run.log')
        runner = unittest.TextTestRunner(
            resultclass=_TimingResult, verbosity=2,
            stream=_Tee(sys.stderr, log_fh) if log_fh is not None else sys.stderr,
        )
        t0 = time.perf_counter()
        try:
            result = runner.run(suite)
        finally:
            if log_fh is not None:
                log_fh.close()
        merged = {
            'total_wall': time.perf_counter() - t0,
            'test_count': result.testsRun,
            'failures': len(result.failures),
            'errors': len(result.errors),
            'per_module': _per_module(result.timings),
            'fixtures': {k: v for k, v in result.fixtures.items() if v >= 0.05},
            'failed_ids': _failed_ids(result),
            'successful': result.wasSuccessful(),
        }
    else:
        modules = _discover_modules()
        weights = _module_weights(modules, _load_history())
        shards, load = _pack_shards(modules, jobs, weights, reverse=args.reverse_shards)
        _assert_no_module_was_dropped(modules, shards)
        print(f'{STATUS_PREFIX} {len(modules)} modules over {len(shards)} shards, '
              f'planned {[round(x) for x in load]}s', file=sys.stderr, flush=True)
        merged = _run_sharded(shards, log_dir)
        jobs = len(shards)

    foreign = _foreign_cpu(cpu_before, _cpu_snapshot(), merged['total_wall'])
    per_module = {k: round(v, 2) for k, v in merged['per_module'].items()}
    per_fixture = {k: round(v, 2) for k, v in merged['fixtures'].items()}
    commit, dirty = _git_info()
    record = {
        'ts_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'git_commit': commit,
        'git_dirty': dirty,
        'tier': tier,
        'jobs': jobs,
        # Only meaningful when jobs > 1. A reverse run is a correctness check, not a
        # performance measurement - see _passing_same_tier.
        'shard_order': shard_order,
        'host': platform.node(),
        'python': platform.python_version(),
        'total_wall': round(merged['total_wall'], 2),
        # Sum of per-test and setUpClass wall time. Compare it only against runs with the
        # same `jobs` - see the note in _status_line for why it is not a -j-independent
        # figure. total_wall is what a person waits through.
        'total_charged': round(sum(per_module.values()) + sum(per_fixture.values()), 2),
        'test_count': merged['test_count'],
        'failures': merged['failures'],
        'errors': merged['errors'],
        'per_module': per_module,
        # setUpClass/tearDownClass cost, which per_module cannot see. See _TimingResult.
        'per_module_fixture': per_fixture,
    }
    if foreign is not None:
        # Written only where it could be measured, so a record without these keys means
        # "unknown", never "the box was idle". Nothing may read a missing key as zero.
        record['cpu_count'] = os.cpu_count() or 1
        record['foreign_cpu_s'] = round(foreign[0], 2)
        record['foreign_cpu_share'] = round(foreign[1], 4)
    failed_ids = merged.get('failed_ids', [])
    if failed_ids:
        # Written only on a red run, so `grep failed_ids` over the history file lists every
        # one of them. A green record carries no such key and nothing may require it.
        # Sanitized on the way into the file and nowhere else: the stderr lines below keep
        # the full subTest suffix, which is what a developer debugging the red run needs,
        # while the committed file gets only bare ids.
        record['failed_ids'] = _history_safe_ids(failed_ids)[:MAX_FAILED_IDS]

    # Baseline is computed from prior runs only; this run is appended afterward so it does
    # not compare against itself. A failed run is still recorded (for history) but never
    # feeds a future baseline (_passing_same_tier filters it out).
    prior_passing = _passing_same_tier(_load_history(), tier, jobs, shard_order)
    line = _status_line(record, prior_passing)
    try:
        _append_history(record)
    except OSError as e:
        line += f"  (history write failed: {e})"

    # Named before the status line, never folded into it: the status line is the one an
    # agent relays verbatim, and burying a failure list inside it would cost it that job.
    for tid in failed_ids:
        print(f'{STATUS_PREFIX} FAILED {tid}', file=sys.stderr, flush=True)
    if log_dir:
        print(f'{STATUS_PREFIX} output captured to '
              f'{os.path.relpath(log_dir, _REPO)}/', file=sys.stderr, flush=True)

    print(line, file=sys.stderr, flush=True)

    # Timing is non-fatal: exit code mirrors the unittest result, never the timing check.
    sys.exit(0 if merged['successful'] else 1)


if __name__ == '__main__':
    main()
