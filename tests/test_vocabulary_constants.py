"""A status or event type is compared against its constant, never against a re-typed word.

CLAUDE.md, "one flag, one meaning": compare statuses and event types only against the
constants in app/database.py. The incident behind the rule is a terminal-event check that
compared `event_type` against `'COMPLETED'` instead of `RECORDING_COMPLETE`, which matched
nothing forever without erroring. The rule had no enforcement, and Account.status had no
constants at all - `'OK'`, `'ERROR'`, `'SYNCING'` were typed at a dozen sites across Python,
Jinja and JS - until dev/changelog/1246 added ACCOUNT_STATUS_* and SYNC_LOG_*, gave the
templates one Jinja global per family and the browser one meta tag (base.html status-vocab,
util.js statusVocab()), and wrote this scan.

Four surfaces, one vocabulary (app/database.py::status_vocabularies() plus the event-type
constants, which spell their own name):

  * app/ Python: a comparison, a filter_by()/constructor keyword, an .in_() list or an
    assignment on `.status` / `.event_type` whose literal is a vocabulary word is an
    offender. app/migrations.py is exempt wholesale - a step compares against the strings
    as they existed historically. Anywhere else, `# vocab-literal-ok: <reason>` on the line.
  * templates/: `status == 'WORD'` is an offender; the one spelling is `REC_STATUS.WORD`,
    `ACCOUNT_STATUS.WORD`, and every such reference must name a member the family has -
    Jinja renders an unknown attribute as Undefined, which compares false, silently.
  * static/js/: `x.status === 'WORD'` is an offender; the spelling is
    `statusVocab('FAMILY').WORD`. `// vocab-literal-ok: <reason>` on the line escapes.
  * The constants themselves: unique within a family, and every event type equal to its
    own name, so a rename cannot leave a stale spelling behind.

Run standalone:
  python3 -m unittest tests.test_vocabulary_constants
"""
import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import database as dbmod  # noqa: E402
from app.database import status_vocabularies  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, 'app')
TPL_DIR = os.path.join(ROOT, 'templates')
JS_DIR = os.path.join(ROOT, 'static', 'js')

MARKER = 'vocab-literal-ok'
#: Files a literal is legitimate in: migrations compare against history, and database.py
#: is where the words are declared.
EXEMPT_FILES = frozenset({'app/migrations.py', 'app/database.py'})
#: The attributes the scan keys on. `status` is every status column; `event_type` every
#: event table's type column.
KEYED_ATTRS = frozenset({'status', 'event_type'})


def event_type_constants():
    """{name: value} for every module-level constant in app/database.py that spells its own
    name - the event-type families (RECORDING_*, CHANNEL_*, GROUP_*, ...)."""
    return {name: value for name, value in vars(dbmod).items()
            if isinstance(value, str) and name.isupper() and value == name}


def vocabulary():
    """Every word a status or event-type column may hold, as a set."""
    words = set(event_type_constants().values())
    for members in status_vocabularies().values():
        words.update(members.values())
    return words


def _walk(root, ext):
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(ext):
                yield os.path.join(dirpath, f)


def _rel(path):
    return os.path.relpath(path, ROOT)


def _literal_words(node, words):
    """The vocabulary words spelled as string constants directly in `node` - a Constant,
    or a Tuple/List of them (an `in (...)` or `.in_([...])` operand)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if node.value in words else []
    if isinstance(node, (ast.Tuple, ast.List)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
                and e.value in words]
    return []


def _is_keyed(node):
    return isinstance(node, ast.Attribute) and node.attr in KEYED_ATTRS


def find_python_literal_sites(source, words):
    """[(lineno, word)] for every site in `source` that pins a `.status`/`.event_type` to a
    vocabulary word by its spelling rather than its constant."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            if any(_is_keyed(op) for op in operands):
                for op in operands:
                    for w in _literal_words(op, words):
                        found.append((node.lineno, w))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in KEYED_ATTRS:
                    for w in _literal_words(kw.value, words):
                        found.append((node.lineno, w))
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ('in_', 'notin_') \
                    and _is_keyed(func.value):
                for arg in node.args:
                    for w in _literal_words(arg, words):
                        found.append((node.lineno, w))
        elif isinstance(node, ast.Assign):
            if any(_is_keyed(t) for t in node.targets):
                for w in _literal_words(node.value, words):
                    found.append((node.lineno, w))
    return sorted(set(found))


_TPL_LITERAL_RE = re.compile(
    r"(?:status|\bst)\s*(?:==|!=|\bin\b|\bnot in\b)\s*\(?\s*'([A-Z_]+)'(?:\s*,\s*'([A-Z_]+)')*")
_TPL_REF_RE = re.compile(r'\b(REC_STATUS|TEST_STATUS|OD_JOB_STATUS|ACCOUNT_STATUS|SYNC_LOG|'
                         r'EPG_STATUS|EPG_SCAN|JOB_RUN)\.([A-Z_]+)')
_JS_LITERAL_RE = re.compile(r"\.(?:status|last_test_status|event_type)\s*(?:===|!==)\s*'([A-Z_]+)'")


def find_template_literal_sites(text, words):
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in _TPL_LITERAL_RE.finditer(line):
            for w in re.findall(r"'([A-Z_]+)'", m.group(0)):
                if w in words:
                    found.append((i, w))
    return found


def find_template_family_refs(text):
    return [(i, m.group(1), m.group(2))
            for i, line in enumerate(text.splitlines(), 1)
            for m in _TPL_REF_RE.finditer(line)]


def find_js_literal_sites(text, words):
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        if MARKER in line:
            continue
        for m in _JS_LITERAL_RE.finditer(line):
            if m.group(1) in words:
                found.append((i, m.group(1)))
    return found


class VocabularyConstantsTests(unittest.TestCase):

    def test_every_family_is_unique_within_itself(self):
        for family, members in status_vocabularies().items():
            values = list(members.values())
            self.assertEqual(len(values), len(set(values)),
                             f'{family} spells one word under two names: {members}')

    def test_every_event_type_spells_its_own_name(self):
        consts = event_type_constants()
        self.assertGreater(len(consts), 60, 'the event-type scan found almost nothing')
        for name in ('RECORDING_FAILED', 'GROUP_FAILOVER', 'CAPTURE_COMPLETE'):
            self.assertIn(name, consts)

    def test_the_vocabulary_is_not_empty(self):
        words = vocabulary()
        for w in ('SYNCING', 'IN_PROGRESS', 'RECORDING_FAILED', 'PARTIAL'):
            self.assertIn(w, words)


class PythonLiteralStatusTests(unittest.TestCase):

    def test_app_compares_against_constants_only(self):
        words = vocabulary()
        offenders = []
        for path in _walk(APP_DIR, '.py'):
            rel = _rel(path)
            if rel in EXEMPT_FILES:
                continue
            with open(path, encoding='utf-8') as fh:
                source = fh.read()
            raw = source.splitlines()
            for lineno, word in find_python_literal_sites(source, words):
                if MARKER in raw[lineno - 1]:
                    continue
                offenders.append(f'{rel}:{lineno}: {word!r}')
        self.assertEqual(
            offenders, [],
            'a status or event type compared, filtered, assigned or constructed from a '
            're-typed literal instead of its app/database.py constant - the shape that '
            'matched nothing forever without erroring. Use the constant, or mark the line '
            f'`# {MARKER}: <reason>`:\n' + '\n'.join(offenders))


class PythonScanCorrectnessTests(unittest.TestCase):
    WORDS = frozenset({'SYNCING', 'FAILED', 'RECORDING_FAILED'})

    def test_a_comparison_is_found(self):
        src = "if account.status == 'SYNCING':\n    pass\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'SYNCING')])

    def test_a_membership_test_is_found(self):
        src = "ok = rec.status in ('FAILED', 'OTHER')\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'FAILED')])

    def test_a_filter_by_keyword_is_found(self):
        src = "rows = Account.query.filter_by(status='SYNCING').all()\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'SYNCING')])

    def test_an_in_list_is_found(self):
        src = "q = q.filter(Log.status.in_(['FAILED']))\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'FAILED')])

    def test_an_assignment_is_found(self):
        src = "account.status = 'SYNCING'\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'SYNCING')])

    def test_an_event_type_keyword_is_found(self):
        src = "RecordingEvent.query.filter_by(event_type='RECORDING_FAILED')\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [(1, 'RECORDING_FAILED')])

    def test_a_constant_comparison_is_not_an_offender(self):
        src = "if account.status == ACCOUNT_STATUS_SYNCING:\n    pass\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [])

    def test_a_word_on_an_unrelated_attribute_is_not_an_offender(self):
        """'ERROR' is also a log level; only the keyed attributes count."""
        src = "if rec['level'] == 'FAILED' or alert.severity == 'FAILED':\n    pass\n"
        self.assertEqual(find_python_literal_sites(src, self.WORDS), [])

    def test_the_scan_still_sees_the_real_tree(self):
        """The app tree is clean, so prove the scanner sees real code by counting the
        constant-based comparisons it would have flagged had they been literals."""
        with open(os.path.join(APP_DIR, 'accounts.py'), encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        # Re-spell the constant as its word everywhere but the import that binds it.
        literalized = '\n'.join(
            line if 'import' in line or line.strip().startswith('ACCOUNT_STATUS')
            else re.sub(r'\bACCOUNT_STATUS_SYNCING\b', "'SYNCING'", line)
            for line in lines)
        self.assertGreater(len(find_python_literal_sites(literalized, {'SYNCING'})), 0)


class TemplateLiteralStatusTests(unittest.TestCase):

    def _templates(self):
        for path in _walk(TPL_DIR, '.html'):
            with open(path, encoding='utf-8') as fh:
                yield _rel(path), fh.read()

    def test_templates_compare_against_the_family_globals_only(self):
        words = vocabulary()
        offenders = [f'{rel}:{i}: {w!r}'
                     for rel, text in self._templates()
                     for i, w in find_template_literal_sites(text, words)]
        self.assertEqual(
            offenders, [],
            "a template compares a status against a re-typed word; the one spelling is the "
            "family global (`rec.status == REC_STATUS.FAILED`, `st == ACCOUNT_STATUS.OK`):\n"
            + '\n'.join(offenders))

    def test_every_family_reference_names_a_member(self):
        """Jinja renders `ACCOUNT_STATUS.NOPE` as Undefined, which compares false and raises
        nothing - exactly the silent mismatch the globals exist to prevent."""
        families = status_vocabularies()
        offenders = [f'{rel}:{i}: {family}.{member}'
                     for rel, text in self._templates()
                     for i, family, member in find_template_family_refs(text)
                     if member not in families[family]]
        self.assertEqual(offenders, [], 'a template names a status the family does not '
                                        'declare:\n' + '\n'.join(offenders))

    def test_the_template_scan_sees_the_real_tree(self):
        refs = [r for _rel, text in self._templates() for r in find_template_family_refs(text)]
        self.assertGreater(len(refs), 40, 'the family-reference scan found almost nothing')
        self.assertEqual(find_template_literal_sites("{% if st == 'OK' %}", {'OK'}), [(1, 'OK')])
        self.assertEqual(find_template_literal_sites(
            "{% elif r.status in ('IN_PROGRESS', 'PAUSED') %}", {'PAUSED'}), [(1, 'PAUSED')])


class JsLiteralStatusTests(unittest.TestCase):

    def test_scripts_compare_against_status_vocab_only(self):
        words = vocabulary()
        offenders = []
        for path in _walk(JS_DIR, '.js'):
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            offenders.extend(f'{_rel(path)}:{i}: {w!r}'
                             for i, w in find_js_literal_sites(text, words))
        self.assertEqual(
            offenders, [],
            "a script compares a status against a re-typed word; the one spelling is "
            f"`statusVocab('FAMILY').WORD` (util.js), or `// {MARKER}: <reason>` on the "
            "line:\n" + '\n'.join(offenders))

    def test_the_js_scan_sees_a_literal(self):
        self.assertEqual(find_js_literal_sites("if (r.status === 'SCHEDULED') x();",
                                               {'SCHEDULED'}), [(1, 'SCHEDULED')])
        self.assertEqual(find_js_literal_sites(
            f"if (r.status === 'SCHEDULED') x(); // {MARKER}: why", {'SCHEDULED'}), [])

    def test_base_html_serves_every_family(self):
        with open(os.path.join(TPL_DIR, 'base.html'), encoding='utf-8') as fh:
            self.assertIn('name="status-vocab"', fh.read())
        with open(os.path.join(JS_DIR, 'util.js'), encoding='utf-8') as fh:
            self.assertIn('function statusVocab(', fh.read())


if __name__ == '__main__':
    unittest.main(verbosity=2)
