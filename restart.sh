#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/dvr.log"

FORCE=0
for arg in "$@"; do
    case "$arg" in
        -f|--force) FORCE=1 ;;
        *) echo "Usage: ./restart.sh [-f|--force]" >&2; exit 2 ;;
    esac
done

DVR_OUTPUT_DIR="$(cd "$SCRIPT_DIR" && python3 -c 'from app.config import load_config; print(load_config()["recording"]["dvr_output_dir"])' 2>/dev/null)"

# Busy guard. A restart kills capture, concatenation AND conversion: run.py's SIGTERM
# handler terminates recorder children and live conversions, and the orphan sweep below
# matches the conversion ffmpeg too, since do_postprocess writes into the same output
# dir. Startup recovery re-arms what it can, but a restart mid-work still costs a resume
# cycle, so refuse by default and make the operator opt in with --force. No interactive
# prompt: agents invoke this non-interactively and a prompt would hang.
#
# The guard only applies while run.py is actually alive. A busy-looking row (IN_PROGRESS,
# CONCATENATING, ...) with no process behind it can only be stale - left by a crash or a
# kill outside this script's control - and nothing is left running to interrupt, so
# refusing just blocks recovery from the exact crash that produced the stale row
# (dev/docs/BUGS.md 2026-08-16).
#
# check_busy.py is consulted even under --force, and its output printed even when nothing
# blocks: --force is exactly when the operator most needs to see what they are interrupting.
# It reports more than recordings - a health check run, a single-channel test, a search
# index rebuild and an account sync all block too (dev/changelog/732).
#
# BUSY_TEXT is the human half of that output: check_busy.py's trailing `blocking-kinds:`
# line is parsed below rather than shown, so it is stripped from everything printed.
if pgrep -f "python.*run\.py" > /dev/null 2>&1; then
    BUSY_ROWS="$(cd "$SCRIPT_DIR" && python3 tools/check_busy.py)" && BUSY=0 || BUSY=1
    BUSY_TEXT="$(echo "$BUSY_ROWS" | grep -v '^blocking-kinds:' || true)"
    if [ "$FORCE" -eq 0 ] && [ "$BUSY" -eq 1 ]; then
        echo "Refusing to restart - work is in flight:"
        echo "$BUSY_TEXT"
        echo
        # The stale-row hint speaks for recordings only, so it is printed only when a
        # recording is what blocked. A health check's probe and a sync write nowhere near
        # DVR_OUTPUT_DIR, so this sweep says nothing about either, and printing "may be
        # stale" for one of those would be actively wrong. check_busy.py's blocking-kinds
        # line is what distinguishes them (dev/changelog/732).
        if echo "$BUSY_ROWS" | grep -q '^blocking-kinds:.*recordings'; then
            if [ -n "$DVR_OUTPUT_DIR" ] && pgrep -f "ffmpeg.*$DVR_OUTPUT_DIR" > /dev/null 2>&1; then
                echo "An ffmpeg process is running under $DVR_OUTPUT_DIR,"
                echo "so restarting will kill live work."
            else
                echo "No ffmpeg process is running under ${DVR_OUTPUT_DIR:-the DVR output dir},"
                echo "so this row may be stale from an earlier kill."
            fi
            echo
        fi
        echo "Restarting interrupts the work above."
        echo "Wait for it to finish, or: ./restart.sh --force"
        exit 1
    elif [ -n "$BUSY_TEXT" ]; then
        echo "$BUSY_TEXT"
    fi
fi

# Kill any running instance and wait for it to fully exit before starting a new
# one. A fixed sleep isn't reliable: APScheduler + active ffmpeg/recorder threads
# can take longer than 1s to shut down, and starting the new process while the old
# one is still alive causes both to "resume" any in-progress recording, spawning
# duplicate ffmpeg processes.
if pgrep -f "python.*run\.py" > /dev/null 2>&1; then
    echo "Stopping existing run.py..."
    pkill -f "python.*run\.py"

    for i in $(seq 1 30); do
        pgrep -f "python.*run\.py" > /dev/null 2>&1 || break
        sleep 1
    done

    if pgrep -f "python.*run\.py" > /dev/null 2>&1; then
        echo "Existing run.py did not exit after 30s, sending SIGKILL..."
        pkill -9 -f "python.*run\.py"
        sleep 1
    fi
fi

# Backstop: run.py's SIGTERM handler kills its own ffmpeg children, but if the
# process had to be force-killed above (-9 can't be caught) any ffmpeg it owned
# is now orphaned and will keep writing to a segment file the new process
# won't know about. Sweep for leftover ffmpeg processes writing into the DVR
# output dir and kill them too. (DVR_OUTPUT_DIR is resolved above the busy guard so
# both the guard's liveness message and this sweep use the same value.)
if [ -n "$DVR_OUTPUT_DIR" ] && pgrep -f "ffmpeg.*$DVR_OUTPUT_DIR" > /dev/null 2>&1; then
    echo "Killing orphaned ffmpeg process(es) writing into $DVR_OUTPUT_DIR..."
    pkill -9 -f "ffmpeg.*$DVR_OUTPUT_DIR"
fi

# Launch from the project directory so relative imports and config paths work
echo "Starting ChannelBin..."
cd "$SCRIPT_DIR"
nohup python3 run.py >> "$LOG_FILE" 2>&1 &
echo "Started (PID $!). Logs: $LOG_FILE"
