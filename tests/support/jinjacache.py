"""Share one compiled-template cache across the suite's many throwaway apps.

Every DB-backed test builds its own app via make_test_app(), and every Flask app builds
its own Jinja Environment. An Environment compiles a template from source the first time
it is rendered, so the suite lexed, parsed and code-generated the same ~50 templates once
per app - 566 apps in a full run. Profiling the render-heavy modules showed Jinja's lexer
and node visitors at the top of the list, above any application code.

Jinja already has the fix built in: a BytecodeCache, keyed on template name plus a
checksum of the source. Handing every app the same one means each template is compiled
once per process instead of once per app. Measured on the full suite: 122s -> 104s.

TEST-ONLY, and unlike tests/support/routecache.py this is not a monkeypatch of anybody's
internals - `Flask.jinja_options` is the documented way to configure the environment, and
`bytecode_cache` is a documented Environment argument. Production is untouched: it builds
one app and renders each template once anyway, so there is nothing to win there.

Staleness is not a risk worth engineering around: the cache key includes
`get_source_checksum(source)`, so editing a template produces a different bucket and the
new source is compiled. The cache directory is a fresh temp dir per process and is removed
at exit, so no state survives a run either. tests/test_jinja_cache_fidelity.py asserts
both halves of that.
"""
import atexit
import shutil
import sys
import tempfile

_cache_dir = None
_original_options = None


def install():
    """Attach a process-wide bytecode cache to every Flask app built from here on.

    Returns True if installed. Never raises and never fails a run: this is a speed
    optimization, and the suite is correct without it - just ~18s slower. A failure to
    install says so on stderr rather than degrading silently (CLAUDE.md: nothing silent).
    """
    global _cache_dir, _original_options
    if _original_options is not None:
        return True

    try:
        from flask import Flask
        from jinja2 import FileSystemBytecodeCache
    except ImportError as exc:
        print(f'[jinjacache] NOT installed: {exc}. The test suite will still run '
              f'correctly, just slower.', file=sys.stderr)
        return False

    _cache_dir = tempfile.mkdtemp(prefix='dvr_test_jinja_bc_')
    atexit.register(shutil.rmtree, _cache_dir, True)

    # Set on the class, not an instance: create_jinja_environment() copies
    # self.jinja_options at env-build time, so every app create_app() makes from now on
    # picks this up, and an app that set its own jinja_options would still win.
    _original_options = Flask.jinja_options
    Flask.jinja_options = {**_original_options,
                           'bytecode_cache': FileSystemBytecodeCache(_cache_dir)}
    return True


def uninstall():
    """Restore Flask's original jinja_options. Used by the fidelity test to build a
    known-uncached app to compare against."""
    global _original_options
    if _original_options is None:
        return
    from flask import Flask
    Flask.jinja_options = _original_options
    _original_options = None


def is_installed():
    return _original_options is not None
