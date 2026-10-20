"""The capture tier: real recordings against the simulator (DESIGN-simulator.md §4,
dev/changelog/1223).

Each scenario drives the unmodified recorder - `start_recording` from a real scheduler job,
the watchdog, the join and the conversion, with real ffmpeg - against a simulated provider
over loopback, injects a fault a real provider has produced, and checks what ChannelBin says
about the recording against the simulator's answer key (sim/answer_key.py).

The scenario modules are named `scenario_*.py`, never `test_*.py`, so the default suite's
discovery (`tests/test_*.py`) never collects them: they take minutes, and they run from
their own runner, `./run_tests.sh --capture`. `tests/test_capture_smoke.py` is the short
set the default suite does run. `tests/test_capture_tier_layout.py` holds both halves.
"""
