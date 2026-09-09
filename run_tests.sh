#!/usr/bin/env bash
# Regression test runner (standalone - run before deploying when you choose to).
#
#   ./run_tests.sh              Runs the full suite, split across 3 worker processes (~206s).
#   ./run_tests.sh -j 1         Runs everything in one process (~386s) - the original path.
#   ./run_tests.sh -j 6         More shards. Each worker peaks around 140MB.
#   ./run_tests.sh --reverse-shards   A different, equally valid shard split. Use it to prove
#                               a green run is order-independent and not just lucky.
#
# Every form runs the SAME tests: the split is by whole module, and a module that lands in
# no shard is a hard error rather than a quietly smaller suite. Reach for `-j 1` when
# debugging anything that smells order-dependent, and see dev/docs/PERF-test-suite.md for
# where the suite's time actually goes.
#
# Playwright (browser/UI tests) is separate: `npx playwright test`, never run from here.
# No restart.sh hook by design.
#
# Runs through tests/support/timing.py (not raw `unittest discover`): same discovery and
# -v per-test output, but it times the run, appends a record to tests/timing_history.jsonl,
# and prints a `[SUITE-TIMING]` INFO/WARNING line at the end (rolling baseline on wall clock,
# hard ceiling and drift check on ms/test, all segmented by shard count). The line also names
# how much of the box went to work that was not this suite, which is the other reason a wall
# clock moves without anything getting slower. Timing is non-fatal - the exit code still
# mirrors pass/fail.
#
# A red run leaves evidence behind rather than only a count: each failing test id is printed
# on its own `[SUITE-TIMING] FAILED` line and stored in the history record, and every
# worker's verbose output is captured to dev/test-logs/<timestamp>/ (gitignored, last 10
# runs kept). So a shard that fails and then reruns green can still be identified after the
# fact instead of re-run and guessed at.
set -euo pipefail
cd "$(dirname "$0")"

exec python3 -m tests.support.timing "$@"
