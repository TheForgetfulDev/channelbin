#!/usr/bin/env bash
# Regression test runner (standalone - run before deploying when you choose to).
#
#   ./run_tests.sh              Runs the full suite, split across 4 worker processes (~297s).
#   ./run_tests.sh -j 1         Runs everything in one process - the original path.
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

# Roughly 408 tests gate themselves on shutil.which('ffmpeg')/('ffprobe'), and a skip is not
# a failure - so with neither on PATH the suite finishes in a fraction of the time, reports
# OK, and has exercised no capture, probe, conversion or screenshot code at all. That is the
# same silent-suite-shrinker that shipped in CI for months (dev/changelog/907), and it came
# back the moment this box's ffmpeg moved out of /usr/bin: a shell holding a PATH from before
# the move finds nothing, and says nothing (dev/changelog/917).
#
# So refuse, rather than run a smaller suite that looks identical to a whole one. Skipping
# them is still allowed - it just has to be asked for, which is the difference between a
# choice and an accident.
#
# Both checks run before either refuses, so a fresh checkout missing both hears about both
# at once rather than one per attempt.
refused=""
if [ "${CHANNELBIN_ALLOW_MISSING_FFMPEG:-}" != "1" ]; then
    missing=""
    for bin in ffmpeg ffprobe; do
        command -v "$bin" >/dev/null 2>&1 || missing="$missing $bin"
    done
    if [ -n "$missing" ]; then
        echo "run_tests.sh: not on PATH:$missing" >&2
        echo "  Around 408 tests gate on these and would skip themselves, so this run would" >&2
        echo "  report green over a much smaller suite. Refusing instead." >&2
        echo "  PATH=$PATH" >&2
        echo "  Install ffmpeg (it ships ffprobe), or set CHANNELBIN_ALLOW_MISSING_FFMPEG=1 to" >&2
        echo "  run the rest of the suite deliberately." >&2
        refused=1
    else
        # Not fatal: 6.1 measured identical to 7.1 and a contributor's distro decides this.
        # But ChannelBin targets 7.1 (README.md), so a run on anything else says which series
        # it actually measured rather than leaving it to be inferred.
        series="$(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"
        case "$series" in
            7.1.*|n7.1.*) ;;
            *) echo "run_tests.sh: note - ffmpeg $series, not the 7.1 series this project" \
                    "targets. Results still count; they were just measured elsewhere." >&2 ;;
        esac
    fi
fi

# The same trap for the client-side suite: every jsdom-backed test gates on `node` being on
# PATH and node_modules/jsdom existing, and node_modules is gitignored - so a fresh clone, a
# `git clean -fdx`, or a node upgrade that emptied it runs none of them and still reports OK
# (dev/changelog/1135). CI is safe only because its workflow runs `npm ci` first.
if [ "${CHANNELBIN_ALLOW_MISSING_NODE:-}" != "1" ]; then
    missing=""
    command -v node >/dev/null 2>&1 || missing="$missing node"
    [ -d node_modules/jsdom ] || missing="$missing node_modules/jsdom"
    if [ -n "$missing" ]; then
        echo "run_tests.sh: missing:$missing" >&2
        echo "  Every client-side (jsdom) test gates on these and would skip itself, so this" >&2
        echo "  run would report green without driving any of the app's JavaScript. Refusing" >&2
        echo "  instead." >&2
        echo "  PATH=$PATH" >&2
        echo "  node_modules looked for in: $(pwd)" >&2
        echo "  Install node, run \`npm ci\` in the repo root, or set" >&2
        echo "  CHANNELBIN_ALLOW_MISSING_NODE=1 to run the rest of the suite deliberately." >&2
        refused=1
    fi
fi

[ -z "$refused" ] || exit 1

exec python3 -m tests.support.timing "$@"
