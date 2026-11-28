"""Which repo files each test module reads: the map a task's tests are picked from.

`./run_tests.sh --map PATH` runs the whole suite with this recorder in every worker and writes
one JSON map: for every test module, the functions it ran in each repo file, the files it
opened and the folders it listed. The dev box's nightly builds it, and the close-out reads it
to run only the modules that read what a task changed (dev/tools/test_select.py,
dev/changelog/1780).

Three sources, each covering what the others cannot see:

- Code it ran, by function: `sys.monitoring` PY_START gives each code object's file and
  `co_qualname`, once per code object per module. The callback returns DISABLE, so a function
  costs one call per module however often it runs, and `restart_events()` at each module
  switch re-arms them all. Function-level, not file-level, because building a test app runs
  code in some forty app modules, so by file nearly every module "reads" nearly every app
  file and a selection is most of the suite. Jinja names a compiled template's code after its
  source file, so a rendered template is recorded here too.
- Files it opened: an audit hook on `open`. Fixtures, docs, JS and CSS that a test reads as
  text, which no code object names.
- Folders it listed (`os.listdir`, `os.scandir`, `glob.glob`), and a `git ls-files` it ran,
  which covers the whole tree. A module that lists a folder is owed a run when anything in
  that folder changes, a new file included: that is what the tree-scanning invariant tests do.
  A child process's own reads are invisible, so a repo file named in a child's argv (a jsdom
  driver, a tool run as a script) is recorded as opened.

Each test module is imported inside its own window, so its import-time work is its own.
Work a thread left running from an earlier module lands in the next one; that only ever adds
a module to a selection, never drops one.
"""
import os
import sys
import unittest

_TOOL = 'channelbin-testmap'
# Never part of a selection: gitignored, and a change to either reruns the whole suite anyway.
_SKIP_PREFIXES = ('node_modules/', '.git/', 'dev/samples/', 'dev/test-logs/', 'dev/.closeout/')
_LIST_EVENTS = frozenset({'os.listdir', 'os.scandir'})


class ModuleMap:
    """Records, per test module, what it read. One per worker process."""

    def __init__(self, repo):
        self.repo = os.path.abspath(repo) + os.sep
        self.current = None
        self.files = {}     # module -> files it opened (as text, or in a child's argv)
        self.code = {}      # module -> {file: {qualified names of the code it ran}}
        self.dirs = {}
        self._mon = None

    # --- recording ------------------------------------------------------------------------

    def _rel(self, path):
        try:
            full = os.path.abspath(os.fsdecode(path))
        except (TypeError, ValueError):
            return None
        if not full.startswith(self.repo):
            return None
        rel = full[len(self.repo):].replace(os.sep, '/')
        if '__pycache__' in rel or rel.endswith('.pyc') or rel.startswith(_SKIP_PREFIXES):
            return None
        return rel

    def _add_file(self, path):
        rel = self._rel(path)
        if rel:
            self.files[self.current].add(rel)

    def _add_dir(self, path, recursive=False):
        if path in ('', '.'):
            path = os.getcwd()
        full = os.path.abspath(os.fsdecode(path))
        if full + os.sep == self.repo:
            rel = ''
        else:
            rel = self._rel(full)
            if rel is None:
                return
        self.dirs[self.current].add(rel + '/**' if recursive and rel else ('**' if recursive
                                                                          else rel))

    def _on_start(self, code, _offset):
        if self.current is not None:
            name = code.co_filename
            if name.startswith(self.repo):
                rel = self._rel(name)
                if rel:
                    self.code[self.current].setdefault(rel, set()).add(code.co_qualname)
        return self._mon.DISABLE

    def _on_audit(self, event, args):
        if self.current is None:
            return
        if event == 'open':
            path = args[0]
            if isinstance(path, (str, bytes)):
                self._add_file(path)
        elif event in _LIST_EVENTS:
            path = args[0] if args else '.'
            if isinstance(path, (str, bytes)):
                self._add_dir(path)
        elif event == 'glob.glob':
            pattern, recursive = args[0], args[1] if len(args) > 1 else False
            if isinstance(pattern, str):
                head = pattern.split('*', 1)[0].split('?', 1)[0].split('[', 1)[0]
                self._add_dir(os.path.dirname(head) or '.', recursive or '**' in pattern)
        elif event == 'subprocess.Popen':
            self._on_popen(args)

    def _on_popen(self, args):
        argv = args[1] if len(args) > 1 else None
        if isinstance(argv, (str, bytes)):
            argv = [argv]
        if not isinstance(argv, (list, tuple)):
            return
        words = [os.fsdecode(a) for a in argv if isinstance(a, (str, bytes, os.PathLike))]
        if words and os.path.basename(words[0]) == 'git' and 'ls-files' in words:
            self.dirs[self.current].add('**')
        cwd = args[2] if len(args) > 2 and isinstance(args[2], (str, bytes)) else None
        for word in words[1:]:
            if word.startswith('-') or len(word) > 400:
                continue
            path = os.path.join(os.fsdecode(cwd), word) if cwd else word
            if os.path.isfile(path):
                self._add_file(path)

    # --- the window -----------------------------------------------------------------------

    def start(self):
        mon = sys.monitoring
        try:
            mon.use_tool_id(mon.COVERAGE_ID, _TOOL)
        except ValueError as exc:
            raise SystemExit(f'testmap: sys.monitoring coverage slot is taken ({exc}); '
                             'the map cannot be built in this process') from exc
        self._mon = mon
        mon.register_callback(mon.COVERAGE_ID, mon.events.PY_START, self._on_start)
        mon.set_events(mon.COVERAGE_ID, mon.events.PY_START)
        sys.addaudithook(self._on_audit)

    def switch(self, module):
        self.current = module
        self.files.setdefault(module, set())
        self.code.setdefault(module, {})
        self.dirs.setdefault(module, set())
        self._mon.restart_events()

    def stop(self):
        self.current = None
        if self._mon is not None:
            self._mon.set_events(self._mon.COVERAGE_ID, 0)
            self._mon.register_callback(self._mon.COVERAGE_ID, self._mon.events.PY_START, None)
            self._mon.free_tool_id(self._mon.COVERAGE_ID)

    def as_dict(self):
        return {m: {'opened': sorted(self.files[m]), 'dirs': sorted(self.dirs.get(m, ())),
                    'code': {f: sorted(q) for f, q in sorted(self.code.get(m, {}).items())}}
                for m in self.files}


class MappedModule(unittest.TestSuite):
    """One test module, imported and run inside its own recording window."""

    def __init__(self, name, mapper, loader=None):
        super().__init__()
        self.name, self.mapper = name, mapper
        self.loader = loader or unittest.TestLoader()
        self._loaded = False

    def run(self, result, debug=False):
        self.mapper.switch(self.name)
        if not self._loaded:
            self._loaded = True
            self.addTests(self.loader.loadTestsFromName(self.name))
        return super().run(result, debug)
