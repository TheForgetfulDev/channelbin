"""Tier 2 - failure-path observability (dev/changelog/268, chunk 5, defect class J).

CLAUDE.md's rule: "Any code path that sets a FAILED/ERROR state must also emit an
event/alert/log naming why (a FAILED recording with no event renders a blank detail page)."
That is Product Principle 1 in its narrowest, most testable form, and the incident behind it
is a FAILED row whose detail page rendered empty because nothing recorded a reason.

Three layers, cheapest first:

  * A STATIC SCAN over app/ pairing every terminal-status assignment with its surface. This
    is the half that scales: a new FAILED-setting site added anywhere in app/ is covered the
    moment it is written, with no test to remember to add (dev/changelog/718).
  * BEHAVIORAL cases driving the highest-value terminal transitions end to end - concat
    failure, conversion failure, pre-check failure - which prove the surface actually reaches
    the database rather than merely appearing in the source.
  * The two original cases: start_recording() into a missing DVR output dir must set status
    FAILED *and* emit a RECORDING_FAILED event naming why (BUGS.md 2026-06-28 output-dir
    class; the same shape as the 06-29 blank-detail family); and /api/nav-status must stay
    200 and log a warning if the scheduler read raises, never 500 or silently swallow it
    (BUGS.md 2026-07-16 02:42 PM activity-status).
"""
import ast
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Recording, RecordingEvent, RECORDING_FAILED  # noqa: E402

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')

# The terminal statuses a recording can be written into by app/ code. FAILED is the one the
# rule names; ABORTED joins it because it is the other end-of-life write, carries the same
# "row goes terminal, page must say why" hazard, and is already clean - so guarding it costs
# nothing and stops the next teardown path from being the silent one.
_TERMINAL_STATUS_NAMES = frozenset({'REC_STATUS_FAILED', 'REC_STATUS_ABORTED'})

# What counts as bringing the failure forward. add_recording_event() is the canonical home
# (app/database.py); RecordingEvent names the two sites that build the row directly; and
# create_alert covers a failure surfaced as a standing alert rather than a row event. A log
# line alone deliberately does NOT count here: the incident this guards is a blank *detail
# page*, and dvr.log is not on it.
_SURFACE_CALLS = frozenset({'add_recording_event', 'create_alert', 'RecordingEvent'})

# (relative path, enclosing function name) pairs whose surface is genuinely somewhere else.
# Empty today - every terminal-status site in app/ surfaces inside its own function. An entry
# added here must name the indirect surface that was READ and confirmed, not one that looked
# plausible; if the scan goes red, the fix is normally the missing event, not a new entry.
_EXEMPT_TERMINAL_SITES = frozenset()


def _called_name(func):
    """The bare name of a call target: `create_alert` for both `create_alert(...)` and
    `_alerts.create_alert(...)`, since the module alias varies by import site."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _innermost_def_map(tree):
    """Map every AST node to the innermost function definition lexically containing it
    (None for module level). A def's own body maps to that def; the def node itself maps to
    its parent, so a nested closure is never confused with the function around it."""
    owner = {tree: None}

    def walk(node, current):
        for child in ast.iter_child_nodes(node):
            owner[child] = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child)
            else:
                walk(child, current)

    walk(tree, None)
    return owner


def find_terminal_status_sites(source):
    """Return [(lineno, enclosing function name or None)] for every `<x>.status = <TERMINAL>`
    assignment in `source`."""
    tree = ast.parse(source)
    owner = _innermost_def_map(tree)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        name = None
        if isinstance(node.value, ast.Name):
            name = node.value.id
        elif isinstance(node.value, ast.Attribute):
            name = node.value.attr
        if name not in _TERMINAL_STATUS_NAMES:
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == 'status' for t in node.targets):
            continue
        enclosing = owner.get(node)
        sites.append((node.lineno, enclosing.name if enclosing is not None else None))
    return sorted(sites)


def find_silent_failure_sites(source, rel_path, exempt=frozenset()):
    """Return 1-indexed line numbers of terminal-status assignments in `source` whose own
    enclosing function emits no event/alert, and which are not in `exempt`.

    Scoped to the INNERMOST enclosing function on purpose: nearly every one of these sites
    is a `@retry_on_locked` closure, and "the surface is somewhere in the 200-line function
    around it" is not the guarantee the rule asks for. A surface sitting in a sibling closure
    does not count - it is not on this path."""
    tree = ast.parse(source)
    owner = _innermost_def_map(tree)

    surfaced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _called_name(node.func) in _SURFACE_CALLS:
            surfaced.add(id(owner.get(node)))

    silent = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        name = None
        if isinstance(node.value, ast.Name):
            name = node.value.id
        elif isinstance(node.value, ast.Attribute):
            name = node.value.attr
        if name not in _TERMINAL_STATUS_NAMES:
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == 'status' for t in node.targets):
            continue
        enclosing = owner.get(node)
        func_name = enclosing.name if enclosing is not None else None
        if (rel_path, func_name) in exempt:
            continue
        if id(enclosing) in surfaced:
            continue
        silent.append(node.lineno)
    return sorted(silent)


def _app_sources():
    """(relative path, source text) for every .py file under app/."""
    for root, _dirs, files in os.walk(APP_DIR):
        for fn in sorted(files):
            if not fn.endswith('.py'):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, os.path.dirname(APP_DIR))
            with open(path, encoding='utf-8') as fh:
                yield rel, fh.read()


class TerminalStatusSurfaceScanTests(unittest.TestCase):
    """The rule itself, as a scan: no site in app/ may write a recording into a terminal
    status without its own function also emitting the event/alert that says why.

    This is the layer the two pre-existing behavioral cases could not provide. They pin two
    specific paths; the incident class is "any FAILED-setting site, including one added
    tomorrow", and only a scan covers a site nobody thought to write a test for."""

    def test_current_tree_has_no_silent_terminal_status_sites(self):
        silent = []
        for rel, source in _app_sources():
            silent.extend(f'{rel}:{n}'
                          for n in find_silent_failure_sites(source, rel,
                                                             _EXEMPT_TERMINAL_SITES))
        self.assertEqual(silent, [], 'terminal-status write with no event/alert in the same '
                                     'function - a FAILED row whose detail page cannot say why')

    def test_scan_actually_sees_the_known_terminal_sites(self):
        """A scan that matched nothing would pass the test above trivially. Pin the modules
        that really do write terminal statuses, so a parser change that stops matching is
        caught here rather than by the silence it would let through."""
        by_module = {}
        for rel, source in _app_sources():
            sites = find_terminal_status_sites(source)
            if sites:
                by_module[rel] = len(sites)
        for expected in ('app/recorder.py', 'app/watchdog.py', 'app/concatenator.py',
                         'app/postprocessor.py', 'app/scheduler.py',
                         'app/routes/recordings.py'):
            self.assertIn(expected, by_module,
                          f'{expected} writes a terminal recording status; the scan must see it')
        self.assertGreaterEqual(sum(by_module.values()), 16,
                                f'scan found only {sum(by_module.values())} terminal-status '
                                f'sites in app/; it saw 16 when written')


class TerminalStatusScanCorrectnessTests(unittest.TestCase):
    """Regression coverage for the scan itself. A static scan is only worth what its own
    failure modes are worth: one that cannot go red is decoration."""

    def test_site_with_no_surface_is_reported(self):
        source = (
            "def _give_up():\n"
            "    r.status = REC_STATUS_FAILED\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [2])

    def test_event_in_the_same_function_suppresses(self):
        source = (
            "def _give_up():\n"
            "    r.status = REC_STATUS_FAILED\n"
            "    add_recording_event(rid, RECORDING_FAILED, detail='why')\n"
            "    db.session.commit()\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [])

    def test_direct_event_construction_suppresses(self):
        """app/postprocessor.py and app/routes/recordings.py build the row directly instead
        of calling add_recording_event; that is still a surface."""
        source = (
            "def _give_up():\n"
            "    r.status = REC_STATUS_FAILED\n"
            "    db.session.add(RecordingEvent(recording_id=rid, detail='why'))\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [])

    def test_alert_suppresses_via_a_module_alias(self):
        source = (
            "def _give_up():\n"
            "    r.status = REC_STATUS_FAILED\n"
            "    _alerts.create_alert('CONVERSION_FAILED', 'why')\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [])

    def test_a_log_line_alone_does_not_count_as_a_surface(self):
        """dvr.log is not the detail page. The blank-page incident had log output."""
        source = (
            "def _give_up():\n"
            "    log.error('giving up on %d', rid)\n"
            "    r.status = REC_STATUS_FAILED\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [3])

    def test_surface_in_a_sibling_closure_does_not_count(self):
        """The overwhelming majority of these sites are retry_on_locked closures. An event
        emitted by a *different* closure in the same outer function is not on this path."""
        source = (
            "def do_thing():\n"
            "    def _happy():\n"
            "        add_recording_event(rid, RECORDING_COMPLETE, detail='ok')\n"
            "    def _sad():\n"
            "        r.status = REC_STATUS_FAILED\n"
            "        db.session.commit()\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [5])

    def test_surface_in_the_enclosing_function_does_not_count(self):
        source = (
            "def do_thing():\n"
            "    add_recording_event(rid, RECORDING_FAILED, detail='why')\n"
            "    def _sad():\n"
            "        r.status = REC_STATUS_FAILED\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [4])

    def test_aborted_is_scanned_too(self):
        source = (
            "def _cancel():\n"
            "    r.status = REC_STATUS_ABORTED\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [2])

    def test_prose_mentioning_an_event_does_not_suppress(self):
        """The string-masking scans in test_static_invariants.py were each defeated once by a
        comment naming the thing they looked for (dev/changelog/295). Parsing instead of
        matching text makes that structurally impossible - assert it stays that way."""
        source = (
            "def _give_up():\n"
            "    # the caller runs add_recording_event for us\n"
            "    \"\"\"Surfaced by create_alert upstream.\"\"\"\n"
            "    r.status = REC_STATUS_FAILED\n"
        )
        self.assertEqual(find_silent_failure_sites(source, 'app/fake.py'), [4])

    def test_allowlist_is_scoped_to_file_and_function(self):
        source = (
            "def _give_up():\n"
            "    r.status = REC_STATUS_FAILED\n"
        )
        exempt = frozenset({('app/real.py', '_give_up')})
        self.assertEqual(find_silent_failure_sites(source, 'app/real.py', exempt), [])
        self.assertEqual(find_silent_failure_sites(source, 'app/other.py', exempt), [2])


def _enclosing_block(tree):
    """Map every statement to the list of statements it sits in (a body, orelse, finalbody
    or handler body), so "the same block" can be asked of a site."""
    owner = {}
    for node in ast.walk(tree):
        for field in ('body', 'orelse', 'finalbody'):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list):
                for st in stmts:
                    owner[st] = stmts
    return owner


def _names_terminal(node, terminal_names):
    """Whether `node` names one of `terminal_names` - directly, or on either arm of a
    conditional expression (`status = COMPLETED if ok else FAILED` is a FAILED site)."""
    if isinstance(node, ast.IfExp):
        return (_names_terminal(node.body, terminal_names)
                or _names_terminal(node.orelse, terminal_names))
    name = None
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    return name in terminal_names


def _statement_of(node, parents):
    while node is not None and not isinstance(node, ast.stmt):
        node = parents.get(node)
    return node


def find_silent_block_sites(source, terminal_names, surface_calls, surface_targets=()):
    """Return 1-indexed line numbers of terminal writes in `source` whose own BLOCK carries
    no surface. A terminal write is an assignment of a name in `terminal_names` (to a bare
    `status` or to `<x>.status`) or a call handed one as an argument; a surface is a call to
    one of `surface_calls`, or an assignment to a name in `surface_targets`, anywhere in the
    same statement list.

    Block-scoped rather than function-scoped (find_silent_failure_sites above) because the
    channel tester decides FAILED six times inside one 600-line function, and "something
    in that function logs" would be true of every one of them whatever they did. The block
    is the site's own statement list and, when that list is a branch of an if/try, the list
    that statement sits in - a verdict decided in an `else:` and logged by the `if` chain
    right after it is one path, not two."""
    tree = ast.parse(source)
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    block_of = _enclosing_block(tree)

    def _is_surface(st):
        for node in ast.walk(st):
            if isinstance(node, ast.Call) and _called_name(node.func) in surface_calls:
                return True
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in surface_targets for t in node.targets):
                return True
        return False

    silent = []
    for node in ast.walk(tree):
        terminal = False
        if isinstance(node, ast.Assign) and _names_terminal(node.value, terminal_names):
            terminal = any((isinstance(t, ast.Name) and t.id == 'status')
                           or (isinstance(t, ast.Attribute) and t.attr == 'status')
                           for t in node.targets)
        elif isinstance(node, ast.Call):
            terminal = any(_names_terminal(a, terminal_names) for a in node.args)
        if not terminal:
            continue
        st = _statement_of(node, parents)
        block = block_of.get(st)
        if block is None:
            continue
        if any(_is_surface(sib) for sib in block):
            continue
        outer = _statement_of(parents.get(st), parents)
        outer_block = block_of.get(outer) if outer is not None else None
        if outer_block is not None and any(_is_surface(sib) for sib in outer_block):
            continue
        silent.append(node.lineno)
    return sorted(set(silent))


class ChannelTestFailureSurfaceScanTests(unittest.TestCase):
    """The rule's second family: a channel test the tester marks FAILED carries, in the same
    block as the decision, the log line or the error detail that says why. The test row's
    `error_detail` and its log are the surface the channel page shows; a FAILED test with
    neither is the recording family's blank detail page, one table over
    (dev/changelog/1246)."""

    _TERMINAL = frozenset({'TEST_STATUS_FAILED'})
    _SURFACE_CALLS = frozenset({'_append_log'})
    _SURFACE_TARGETS = frozenset({'error_detail', 'quality_fail_msg'})

    def _source(self):
        with open(os.path.join(APP_DIR, 'channel_tester.py'), encoding='utf-8') as fh:
            return fh.read()

    def test_every_failed_decision_in_the_tester_says_why_in_its_own_block(self):
        silent = find_silent_block_sites(self._source(), self._TERMINAL,
                                         self._SURFACE_CALLS, self._SURFACE_TARGETS)
        self.assertEqual(silent, [], 'app/channel_tester.py marks a test FAILED with no '
                                     '_append_log / error_detail in the same block: '
                                     f'lines {silent}')

    def test_the_scan_sees_the_tester_decisions(self):
        tree = ast.parse(self._source())
        sites = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Assign) and _names_terminal(n.value, self._TERMINAL)]
        self.assertGreaterEqual(len(sites), 4,
                                f'found only {len(sites)} TEST_STATUS_FAILED decisions; '
                                'the tester had six when written')


class SearchIndexFailureSurfaceScanTests(unittest.TestCase):
    """Third family: a search index recorded FAILED raises its standing alert in the same
    block. The alert is the only surface - the index has no detail page - and a FAILED
    index sends every search to LIKE, which is the slowness the user would otherwise have
    to diagnose from nothing."""

    _TERMINAL = frozenset({'STATUS_FAILED'})
    _SURFACE_CALLS = frozenset({'_alert_rebuild_failed'})

    def _source(self):
        with open(os.path.join(APP_DIR, 'search_index.py'), encoding='utf-8') as fh:
            return fh.read()

    def test_every_failed_record_alerts_in_its_own_block(self):
        silent = find_silent_block_sites(self._source(), self._TERMINAL, self._SURFACE_CALLS)
        self.assertEqual(silent, [], 'app/search_index.py records an index FAILED with no '
                                     f'alert in the same block: lines {silent}')

    def test_the_scan_sees_both_record_sites(self):
        tree = ast.parse(self._source())
        sites = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and any(_names_terminal(a, self._TERMINAL) for a in n.args)]
        self.assertGreaterEqual(len(sites), 2)


class AccountFailureSurfaceTests(unittest.TestCase):
    """Fourth family: the account page explains a failed sync from `Account.last_error`,
    and the sync history from the log row's `error_message`. Both are written by the one
    function that moves an account to ERROR, `accounts.finalize_sync_state`, and every
    caller that hands it ERROR hands it both messages."""

    def _source(self, rel):
        with open(os.path.join(os.path.dirname(APP_DIR), rel), encoding='utf-8') as fh:
            return fh.read()

    def test_finalize_sync_state_writes_the_reason_beside_the_status(self):
        tree = ast.parse(self._source('app/accounts.py'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'finalize_sync_state')
        targets = {t.attr for n in ast.walk(fn) if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Attribute)}
        self.assertTrue({'status', 'last_error', 'error_message'} <= targets, targets)

    def test_every_error_caller_passes_both_messages(self):
        offenders = []
        for rel in ('app/accounts.py', 'app/routes/accounts.py', 'app/scheduler.py'):
            tree = ast.parse(self._source(rel))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and _called_name(node.func) == 'finalize_sync_state'):
                    continue
                if not any(_names_terminal(a, {'ACCOUNT_STATUS_ERROR'}) for a in node.args):
                    continue
                kws = {kw.arg for kw in node.keywords}
                if not {'account_message', 'log_message'} <= kws:
                    offenders.append(f'{rel}:{node.lineno}')
        self.assertEqual(offenders, [])

    def test_the_scan_sees_the_error_caller(self):
        tree = ast.parse(self._source('app/accounts.py'))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and _called_name(n.func) == 'finalize_sync_state'
                 and any(_names_terminal(a, {'ACCOUNT_STATUS_ERROR'}) for a in n.args)]
        self.assertGreaterEqual(len(calls), 1)


class BlockScanCorrectnessTests(unittest.TestCase):

    def test_a_decision_with_no_surface_in_its_block_is_reported(self):
        src = ("def run():\n"
               "    while True:\n"
               "        if bad:\n"
               "            status = TEST_STATUS_FAILED\n"
               "    _append_log('INFO', 'elsewhere')\n")
        self.assertEqual(find_silent_block_sites(src, {'TEST_STATUS_FAILED'}, {'_append_log'}),
                         [4])

    def test_a_verdict_in_a_branch_logged_by_the_chain_after_it_is_one_path(self):
        src = ("def run():\n"
               "    if cancelled:\n"
               "        status = TEST_STATUS_CANCELLED\n"
               "    else:\n"
               "        status = TEST_STATUS_COMPLETED if ok else TEST_STATUS_FAILED\n"
               "    if status == TEST_STATUS_FAILED:\n"
               "        _append_log('ERROR', 'why')\n")
        self.assertEqual(find_silent_block_sites(src, {'TEST_STATUS_FAILED'}, {'_append_log'}),
                         [])

    def test_a_surface_in_the_same_block_suppresses(self):
        src = ("def run():\n"
               "    if bad:\n"
               "        status = TEST_STATUS_FAILED\n"
               "        _append_log('ERROR', 'why')\n")
        self.assertEqual(find_silent_block_sites(src, {'TEST_STATUS_FAILED'}, {'_append_log'}),
                         [])

    def test_an_assignment_surface_counts(self):
        src = ("def run():\n"
               "    if bad:\n"
               "        error_detail = 'why'\n"
               "        status = TEST_STATUS_FAILED\n")
        self.assertEqual(find_silent_block_sites(src, {'TEST_STATUS_FAILED'}, set(),
                                                 {'error_detail'}), [])

    def test_a_call_handed_the_terminal_name_is_a_site(self):
        src = ("def run():\n"
               "    _record_state(name, STATUS_FAILED, None)\n")
        self.assertEqual(find_silent_block_sites(src, {'STATUS_FAILED'}, {'_alert'}), [2])


class MissingOutputDirTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_missing_dir_marks_failed_with_event(self):
        rec = seed.make_recording(status='SCHEDULED', channel_id=self.ch.id)
        db.session.commit()
        rid = rec.id

        # recorder.load_config() reads the real config.yaml (make_test_app's overrides
        # deliberately don't leak into fresh load_config() calls - prod parity), so patch
        # it to hand start_recording a real config with only dvr_output_dir pointed at a
        # nonexistent path. That trips the missing-dir guard before any ffmpeg spawn.
        import app.recorder as recorder
        from app.config import load_config as _real, _deep_merge
        recorder.load_config = lambda *a, **k: _deep_merge(
            _real(), {'recording': {'dvr_output_dir': '/nonexistent/dvr/path/xyz'}})
        try:
            recorder.start_recording(self.t.app, rid)
        finally:
            recorder.load_config = _real

        # start_recording commits inside its own app-context/session scope, so the
        # outer session's cached row is stale until expired.
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, 'FAILED')
        events = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_FAILED).all()
        self.assertEqual(len(events), 1,
                         'a FAILED recording must emit exactly one RECORDING_FAILED event')
        self.assertIn('does not exist', (events[0].detail or ''))


class ConcatFailureIsObservableTests(unittest.TestCase):
    """The concat give-up path, end to end. A recording whose segments are all gone is the
    cheapest real failure to provoke, and it is a genuine one: it is what a capture that
    never wrote a byte looks like by the time the concatenator sees it."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_no_valid_segments_marks_failed_with_an_event_naming_why(self):
        from app.database import CONCATENATION_DONE, CONCATENATION_FAILED, REC_STATUS_FAILED
        import app.concatenator as concatenator

        rec = seed.make_recording(status='IN_PROGRESS', name='dead concat')
        db.session.commit()
        rid = rec.id

        # Subprocess side effect, irrelevant to the assertion and not free.
        with mock.patch('app.recorder.persist_final_thumbnail'):
            concatenator.do_concatenation(self.t.app, rid)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, REC_STATUS_FAILED)
        events = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=CONCATENATION_FAILED).all()
        self.assertEqual(len(events), 1,
                         'a concat give-up must leave exactly one CONCATENATION_FAILED event')
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rid, event_type=CONCATENATION_DONE).count(), 0,
            'CONCATENATION_DONE means a joined file and nothing else (dev/changelog/1140)')
        self.assertIn('no valid segments', (events[0].detail or '').lower(),
                      f'the event must name the reason, got {events[0].detail!r}')


class ConversionFailureIsObservableTests(unittest.TestCase):
    """The conversion give-up path taken at startup: a row left CONVERTING by a crash whose
    source .ts is gone can never be resumed, so it is failed on the spot. It surfaces twice -
    a CONVERSION_FAILED event on the recording and a standing CONVERSION_FAILED alert - and
    both are asserted, because the alert is the half the detail page does not carry."""

    def setUp(self):
        # resume_in_progress_recordings() sweeps orphaned on-demand jobs at the end, which
        # needs a live scheduler (same reason as tests/test_concat_startup_recovery.py).
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_converting_row_with_missing_source_fails_loudly(self):
        from app.database import Alert, CONVERSION_FAILED, REC_STATUS_FAILED
        from app.scheduler import resume_in_progress_recordings

        rec = seed.make_recording(status='CONVERTING', name='orphaned conversion')
        rec.output_path = os.path.join(self.t._tmpdir, 'gone-forever.ts')
        db.session.commit()
        rid = rec.id

        resume_in_progress_recordings(self.t.app)

        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rid).status, REC_STATUS_FAILED)
        events = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=CONVERSION_FAILED).all()
        self.assertEqual(len(events), 1)
        self.assertIn('missing', (events[0].detail or '').lower(),
                      f'the event must name the reason, got {events[0].detail!r}')
        alerts = Alert.query.filter_by(alert_type='CONVERSION_FAILED').all()
        self.assertEqual(len(alerts), 1,
                         'a conversion that can never resume must also raise a standing alert')


class PreCheckFailureIsObservableTests(unittest.TestCase):
    """A pre-check exists to warn that a recording is about to run against a dead channel.
    A failed pre-check that recorded nothing on the recording would defeat its whole purpose:
    the recording still runs, and nothing on its page says it was warned."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_failed_pre_check_writes_an_event_naming_the_channel(self):
        from datetime import datetime, timedelta
        from app.config import load_config as _real, _deep_merge
        from app.database import PRE_CHECK_FAILED
        import app.channel_tester as tester

        acct = seed.make_account()
        ch = seed.make_channel(acct, stream_id=7, name='Dead Sports HD')
        rec = seed.make_recording(status='SCHEDULED', channel_id=ch.id,
                                  start_time=datetime.utcnow() + timedelta(hours=2))
        db.session.commit()
        rid = rec.id

        failed_test = seed.make_channel_test(ch, status='FAILED',
                                             error_detail='connection refused')
        db.session.commit()
        test_id = failed_test.id

        # run_pre_check re-imports load_config inside the function, so patching the config
        # module reaches it; make_test_app overrides deliberately do not (CLAUDE.md Testing).
        cfg = _deep_merge(_real(), {'channel_testing': {'pre_check': {'enabled': True}}})
        with mock.patch('app.config.load_config', return_value=cfg), \
                mock.patch.object(tester, 'run_channel_test', return_value=test_id):
            tester.run_pre_check(self.t.app, rid)

        db.session.expire_all()
        events = RecordingEvent.query.filter_by(
            recording_id=rid, event_type=PRE_CHECK_FAILED).all()
        self.assertEqual(len(events), 1,
                         'a failed pre-check must record itself on the recording it guards')
        self.assertIn('Dead Sports HD', (events[0].detail or ''),
                      f'the event must name the channel, got {events[0].detail!r}')


class NavStatusResilienceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_nav_status_survives_scheduler_read_failure(self):
        import app.scheduler as sched

        def boom():
            raise RuntimeError('jobstore exploded')

        real = sched.get_scheduler
        sched.get_scheduler = boom
        try:
            with self.assertLogs('app.routes.dashboard', level='WARNING') as cm:
                resp = self.t.client.get('/api/nav-status')
        finally:
            sched.get_scheduler = real

        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertIn('activity', body)
        self.assertEqual(body['activity']['background']['state'], 'hidden')
        self.assertTrue(any('jobstore exploded' in m for m in cm.output),
                        'scheduler read failure must be logged, not swallowed')


if __name__ == '__main__':
    unittest.main(verbosity=2)
