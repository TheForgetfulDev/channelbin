"""Guards dev/changelog/932: ALERT_TYPES' `self_clearing` flag matches the code.

The Alerts page's "Active alerts" card holds the types the app dismisses BY ITSELF once
their condition stops being true, and it deliberately offers no Dismiss - so the flag is
the whole basis of a promise made to the user. If a type is flagged with no clearing path
behind it, its rows sit in that card forever with no way to remove them. If a type has a
clearing path and is NOT flagged, it lands under "Past alerts" where it can be dismissed
while the problem is still happening. That is the exact failure the split was built to stop
(dev/changelog/932): "if something is going to be called `Active Alerts` or even `Still
Happening` then it needs to be limited to items that will clear automatically."

A static scan rather than a behavioral test, for the same reason tests/test_retired_alert_
types.py is one: the clearing paths are spread over eight modules behind four different
helpers, and the failure being guarded against is somebody adding a fifteenth standing alert
and not flagging it - which no single behavioral test would notice.

The scan finds a clearing path two ways:
  1. A call to one of the dismissing helpers naming its type (a literal, or a name imported
     from app/alerts.py - app/toolchain.py and app/auth.py pass module constants, and
     app/notifications.py imports them under an alias).
  2. A function that assigns `.dismissed_at` and names an alert type literally - the two
     hand-rolled dismissers in app/health_score.py, which pre-date the shared helper.

Rule 2 is deliberately broad. It can over-report (a function that dismisses type A while
merely mentioning type B), and that is the safe direction: an over-report fails this test
and a human decides, where a miss ships a card whose name is false.

No network, no real ffmpeg - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_self_clearing_alerts
"""
import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import alerts as alerts_mod  # noqa: E402
from app.alerts import (ALERT_TYPES, RETIRED_ALERT_TYPES,  # noqa: E402
                        SELF_CLEARING_ALERT_TYPES, is_self_clearing)

_APP_DIR = pathlib.Path(__file__).resolve().parent.parent / 'app'

#: function name -> which positional argument names the alert type. Every one of these
#: dismisses the open rows of the type it is handed: dismiss_open_alerts and its
#: notifications.py alias directly, _raise_or_resolve_standing_alert through its
#: `active=False` branch, and dismiss_open_alerts_for_recording by recording id (its type
#: argument is second, which is why this is a map rather than a set).
_DISMISSING_FUNCTIONS = {
    'dismiss_open_alerts': 0,
    '_dismiss_service_alert': 0,
    '_raise_or_resolve_standing_alert': 0,
    'dismiss_open_alerts_for_recording': 1,
}

#: app/routes/alerts.py is the USER dismissing a row by hand, which is the opposite of the
#: app clearing a condition it can still see, so its two routes are not clearing paths.
#:
#: app/alerts.py is deliberately NOT excluded, though its dismiss helpers live there: they
#: take the type as a parameter and name none, so they contribute nothing - while
#: update_storage_path_alert() in the same file DOES name STORAGE_PATH_UNUSABLE and is that
#: type's only clearing path. Skipping the file cost exactly that one type.
_NOT_CLEARING_PATHS = frozenset({'routes/alerts.py'})

#: Names in app/alerts.py bound to an alert type string (STORAGE_PATH_UNUSABLE and friends).
_ALERTS_CONSTANTS = {
    name: value for name, value in vars(alerts_mod).items()
    if isinstance(value, str) and value in ALERT_TYPES
}


def _alerts_import_aliases(tree):
    """{local name: name in app/alerts.py} for this module's `from .alerts import X as Y`.

    app/notifications.py imports both of its types under aliases, so a scan that only
    understood literals and bare names would miss the two service alerts entirely.
    """
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or '').endswith('alerts'):
            for name in node.names:
                aliases[name.asname or name.name] = name.name
    return aliases


def _resolve(node, aliases):
    """The alert type a call argument names, or None if it names none statically."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in ALERT_TYPES else None
    if isinstance(node, ast.Name):
        return _ALERTS_CONSTANTS.get(aliases.get(node.id, node.id))
    return None


def _types_from_dismiss_calls(tree, aliases):
    """[(type, lineno)] for every call to a dismissing helper that names its type."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
        index = _DISMISSING_FUNCTIONS.get(name)
        if index is None:
            continue
        if len(node.args) > index:
            resolved = _resolve(node.args[index], aliases)
            if resolved:
                found.append((resolved, node.lineno))
        for kw in node.keywords:
            if kw.arg == 'alert_type':
                resolved = _resolve(kw.value, aliases)
                if resolved:
                    found.append((resolved, node.lineno))
    return found


def _assigns_dismissed_at(fn):
    for node in ast.walk(fn):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == 'dismissed_at':
                return True
    return False


def _types_from_hand_rolled_dismissers(tree, aliases):
    """[(type, lineno)] for a function that sets `.dismissed_at` and names a type itself."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _assigns_dismissed_at(node):
            continue
        for inner in ast.walk(node):
            resolved = _resolve(inner, aliases)
            if resolved:
                found.append((resolved, inner.lineno))
    return found


def _scan():
    """{alert type: ['app/foo.py:120', ...]} for every type the app clears by itself."""
    sites = {}
    for path in sorted(_APP_DIR.rglob('*.py')):
        rel = str(path.relative_to(_APP_DIR))
        if rel in _NOT_CLEARING_PATHS:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        aliases = _alerts_import_aliases(tree)
        for alert_type, lineno in (_types_from_dismiss_calls(tree, aliases)
                                   + _types_from_hand_rolled_dismissers(tree, aliases)):
            sites.setdefault(alert_type, []).append(f'app/{rel}:{lineno}')
    return sites


class SelfClearingFlagMatchesTheCodeTests(unittest.TestCase):
    def setUp(self):
        self.sites = _scan()
        # A retired type keeps its dismiss call sites so the rows already in the database
        # can still be cleared, but nothing raises it any more, so it can never appear
        # under Active alerts and is not flagged (dev/changelog/928).
        self.cleared = set(self.sites) - RETIRED_ALERT_TYPES

    def test_every_type_the_app_clears_by_itself_is_flagged(self):
        missing = sorted(self.cleared - SELF_CLEARING_ALERT_TYPES)
        detail = '; '.join(f'{t} ({", ".join(self.sites[t])})' for t in missing)
        self.assertEqual(
            [], missing,
            "these types have a clearing path but are not marked self_clearing, so they "
            "render under Past alerts where a problem that is still happening can be "
            "dismissed. Add 'self_clearing': True to the ALERT_TYPES entry. Offenders: "
            + detail)

    def test_no_flagged_type_lacks_a_clearing_path(self):
        stranded = sorted(SELF_CLEARING_ALERT_TYPES - self.cleared)
        self.assertEqual(
            [], stranded,
            'these types are marked self_clearing but nothing in app/ dismisses them, so '
            'their rows would sit under Active alerts - which offers no Dismiss - with no '
            'way for anyone to remove them. Either build the clearing path or drop the '
            f'flag: {stranded}')

    def test_the_scan_finds_the_paths_it_was_built_for(self):
        """A scan that quietly matched nothing would pass both cases above only if the flag
        were also empty, so pin the four call shapes it has to understand: a literal, a
        module constant, an aliased import, and a second-position type argument."""
        self.assertIn('HEALTH_CHECK_WINDOW', self.sites)          # literal
        self.assertIn('EXTERNAL_TOOL_MISSING', self.sites)        # bare imported constant
        self.assertIn('NOTIFICATION_SERVICE_SEND_FAILED', self.sites)  # aliased import
        self.assertIn('CONVERSION_FAILED', self.sites)            # type in argument 2
        self.assertIn('RECORDING_CHANNEL_FAILING', self.sites)    # hand-rolled dismisser


class SelfClearingMembershipTests(unittest.TestCase):
    """The calls this membership was decided on, named individually so a change to any one
    of them is a deliberate edit here rather than a silent shift in what the card holds."""

    def test_a_type_with_no_clearing_path_is_not_self_clearing(self):
        # Nothing re-runs a concatenation that found nothing to concatenate, so this is a
        # record of a loss and is cleared only by deleting the recording (dev/changelog/930).
        self.assertFalse(is_self_clearing('CONCATENATION_FAILED'))
        # _alert_url_drift refreshes a standing row but never dismisses one.
        self.assertFalse(is_self_clearing('PROVIDER_URLS_CHANGED'))

    def test_the_two_ongoing_alerts_clear_themselves(self):
        """Both describe a problem that is still true while the row stands, and both were
        left out of the original split because nothing took either one down. They were
        given clearing paths in dev/changelog/933 - every way out of a connection-slot
        wait, and every way a guide row stops being unable to record - so both now belong
        in the card that promises the problem is still happening. The behavior behind the
        flag is held down by tests/test_ongoing_alerts_clear_themselves.py."""
        self.assertTrue(is_self_clearing('RECORDING_WAITING_FOR_CONNECTION_SLOT'))
        self.assertTrue(is_self_clearing('GROUP_GUIDE_NO_RECORDING_MEMBER'))

    def test_an_unknown_type_is_never_self_clearing(self):
        self.assertFalse(is_self_clearing('NOT_A_REAL_TYPE'))
        self.assertFalse(is_self_clearing(''))

    def test_no_retired_type_is_flagged(self):
        self.assertEqual([], sorted(SELF_CLEARING_ALERT_TYPES & RETIRED_ALERT_TYPES))

    def test_the_flag_is_derived_from_the_registry_not_a_second_list(self):
        self.assertEqual(
            SELF_CLEARING_ALERT_TYPES,
            frozenset(t for t, meta in ALERT_TYPES.items() if meta.get('self_clearing')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
