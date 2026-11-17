"""The real log-to-alert handler, for a test that needs it.

make_test_app() strips it (tests/support/app.py::_strip_alert_handlers), so an ERROR a test
logs writes no Alert row. _AlertHandler is a closure-local class inside _setup_logging and
cannot be imported; calling _setup_logging is how a test gets the genuine article rather
than a stand-in that could drift from it. A cfg with no logging.file keeps it to a
StreamHandler, and logging.basicConfig is a no-op because the root logger already has
handlers.
"""
import logging

import app as app_pkg


def attach():
    """Attach one and return the live handlers; raises if none was attached."""
    app_pkg._setup_logging({'logging': {'level': 'INFO'}})
    handlers = [h for h in logging.getLogger().handlers if type(h).__name__ == '_AlertHandler']
    if not handlers:
        raise AssertionError('_setup_logging did not attach an _AlertHandler - a test that '
                             'depends on it would silently pass without one')
    return handlers


def detach():
    root = logging.getLogger()
    for h in list(root.handlers):
        if type(h).__name__ == '_AlertHandler':
            root.removeHandler(h)
