"""Shared test infrastructure (Tier 2 support scaffolding).

`make_test_app()` is the single app-building path every DB-backed test stands on - a
throwaway Flask app against a temp SQLite file, scheduler suppressed, output dirs and
logging redirected to a temp dir, alert-log handler stripped. It NEVER touches the live
dvr.db, /dvr, or config.yaml. Row factories live in `seed.py`.
"""
from .app import make_test_app, TestApp  # noqa: F401
