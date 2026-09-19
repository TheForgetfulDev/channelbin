"""Guards dev/docs/BUGS.md 2026-09-18 @ 07:38:18 PM: an alert type spelled wrong at a raise or
dismiss site matches nothing in ALERT_TYPES, forever, without erroring.

create_alert() skips a type it does not know (now at WARNING, but only when that line runs),
and has_open_alert() / dismiss_open_alerts*() filter on the type, so a typo there is an alert
that never fires or one that never clears. Most sites name the type as a re-typed string
literal rather than a constant, so this walks every literal handed to one of those helpers
and asserts it is a catalog key.

A static scan rather than a behavioral test, for the reason tests/test_retired_alert_types.py
gives: the sites are spread over a dozen modules and the failure is a new one being added.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_alert_type_literals
"""
import ast
import logging
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.alerts import ALERT_TYPES  # noqa: E402

_APP_DIR = pathlib.Path(__file__).resolve().parent.parent / 'app'

#: helper name -> the positional index its alert type sits at. Every one also accepts it
#: as `alert_type=`.
_TYPE_ARG_INDEX = {
    'create_alert': 0,
    'has_open_alert': 0,
    'dismiss_open_alerts': 0,
    '_raise_or_resolve_standing_alert': 0,
    'dismiss_open_alerts_for_recording': 1,
}

#: Fewer literal sites than this means the scan stopped recognizing the call shape, not that
#: the code stopped using literals. 35 found when this was written.
_MIN_SITES = 30


def _alert_type_literals(tree):
    """[(alert_type, lineno)] for every call to a _TYPE_ARG_INDEX helper whose type is a
    string literal."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        if name not in _TYPE_ARG_INDEX:
            continue
        idx = _TYPE_ARG_INDEX[name]
        candidates = [node.args[idx]] if len(node.args) > idx else []
        candidates += [kw.value for kw in node.keywords if kw.arg == 'alert_type']
        for value in candidates:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append((value.value, node.lineno))
    return found


def _scan_app():
    sites = []
    for path in sorted(_APP_DIR.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        rel = path.relative_to(_APP_DIR.parent)
        sites += [(alert_type, f'{rel}:{lineno}')
                  for alert_type, lineno in _alert_type_literals(tree)]
    return sites


class AlertTypeLiteralTests(unittest.TestCase):
    def test_every_literal_alert_type_is_in_the_catalog(self):
        unknown = [f'{where} {alert_type!r}' for alert_type, where in _scan_app()
                   if alert_type not in ALERT_TYPES]
        self.assertEqual(
            [], unknown,
            'these alert types are not keys of app/alerts.py::ALERT_TYPES, so the alert '
            'never fires (create_alert) or never clears (dismiss_*): ' + '; '.join(unknown))

    def test_scan_still_recognizes_the_call_sites(self):
        sites = _scan_app()
        self.assertGreaterEqual(
            len(sites), _MIN_SITES,
            f'found only {len(sites)} literal alert-type sites in app/ - the helper names '
            'or argument positions in _TYPE_ARG_INDEX have probably drifted')

    def test_scanner_catches_a_misspelled_literal(self):
        tree = ast.parse(
            "create_alert('CONVERSION_FAILD', 't')\n"
            "alerts.dismiss_open_alerts_for_recording(7, 'RECORDING_MOVE_FAILD')\n"
            "_raise_or_resolve_standing_alert(alert_type='SYNC_FAILD', source='s', active=1)\n")
        self.assertEqual(
            ['CONVERSION_FAILD', 'RECORDING_MOVE_FAILD', 'SYNC_FAILD'],
            sorted(t for t, _ in _alert_type_literals(tree)))


class UnknownAlertTypeIsLoudTests(unittest.TestCase):
    def test_unknown_type_logs_a_warning(self):
        from app import alerts
        with self.assertLogs(alerts.log, level=logging.WARNING) as cm:
            alerts.create_alert('NO_SUCH_ALERT_TYPE', 'title')
        self.assertTrue(any('NO_SUCH_ALERT_TYPE' in line for line in cm.output), cm.output)


if __name__ == '__main__':
    unittest.main()
