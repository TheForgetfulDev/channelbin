"""Guards dev/changelog/928: nothing raises the twelve types in RETIRED_ALERT_TYPES.

Each of them reported a fact that is not a problem and that is already shown on the group,
recording, account or page it concerns, so they were dropped when alerts were narrowed to
real problems (dev/changelog/923). In the week measured before the change they were 189 of
198 alerts raised.

A static scan rather than a behavioral test, deliberately: the raise sites were spread over
eight modules, several behind helpers, and the failure being guarded against is somebody
adding one back - which no single behavioral test would notice.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_retired_alert_types
"""
import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.alerts import ALERT_TYPES, RETIRED_ALERT_TYPES  # noqa: E402

_APP_DIR = pathlib.Path(__file__).resolve().parent.parent / 'app'

#: Functions that take an alert type as their first argument and raise it. The standing-alert
#: helper is included because it reaches create_alert() with a variable, so scanning only
#: create_alert() would miss a retired type handed to it (that is how SYNC_STREAM_URLS_
#: CONSTRUCTED was raised).
_RAISING_FUNCTIONS = frozenset({'create_alert', '_raise_or_resolve_standing_alert'})


def _raised_alert_types(tree):
    """[(alert_type, lineno)] for every call that names its type as a string literal, either
    positionally or as `alert_type=` - the shape all fourteen former raise sites used."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        if name not in _RAISING_FUNCTIONS:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            found.append((node.args[0].value, node.lineno))
        for kw in node.keywords:
            if kw.arg == 'alert_type' and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                found.append((kw.value.value, node.lineno))
    return found


class RetiredAlertTypesTests(unittest.TestCase):
    def test_nothing_in_app_raises_a_retired_type(self):
        offenders = []
        for path in sorted(_APP_DIR.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for alert_type, lineno in _raised_alert_types(tree):
                if alert_type in RETIRED_ALERT_TYPES:
                    offenders.append(f'{path.relative_to(_APP_DIR.parent)}:{lineno} {alert_type}')
        self.assertEqual(
            [], offenders,
            'these types are shown on the object they concern, not alerted - if the fact '
            'genuinely needs an alert now, take it out of RETIRED_ALERT_TYPES deliberately '
            'and say why in a changelog. Offenders: ' + '; '.join(offenders))

    def test_every_retired_type_keeps_its_label(self):
        """Rows already in the database still render, so the entry stays in ALERT_TYPES."""
        missing = sorted(RETIRED_ALERT_TYPES - set(ALERT_TYPES))
        self.assertEqual([], missing,
                         f'retired types must keep an ALERT_TYPES label: {missing}')

    def test_no_retired_type_ships_routing_configuration(self):
        """A routing row for a type nothing raises is a switch that cannot do anything."""
        from app.config import _DEFAULTS
        routing = _DEFAULTS['notifications']['routing']
        overlap = sorted(set(routing) & RETIRED_ALERT_TYPES)
        self.assertEqual([], overlap,
                         f'retired types must not have default routing rows: {overlap}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
