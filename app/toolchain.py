"""Which external tools this install actually resolved, and what version they are.

ChannelBin depends on exactly two binaries it does not ship, and there are four real
answers to "which ffmpeg is this" on a given install - this box's system 6.1.1, the
container's 7.1.5, a pip-only install's bundled 7.0.2, or an absolute path the user put in
`ffmpeg.path`. Every behavior this codebase treats as settled fact about capture,
conversion and screenshots was measured against one specific build, so which build is
running is a first-order diagnostic question, and until dev/changelog/910 the app answered
it nowhere: not at startup, not on a page, not as an event, not in an alert.

Which is answered here per binary, because ffmpeg and ffprobe can be missing separately.
They no longer arrive separately: until dev/changelog/911 a pip install supplied a bundled
ffmpeg and no ffprobe, so an install with no system ffmpeg had a working capture path and
a probe path that failed on every call and returned {} - no format detection, no recording
health numbers, no format lock, and nothing anywhere saying why. Dropping that fallback is
what makes "both or neither" true; reporting each one separately is what keeps this honest
if it stops being true (a user pointing ffmpeg.path at a binary with no ffprobe beside it).

A missing tool never stops the app starting. It raises a standing alert instead, so the
problem is loud in the app UI rather than something to dig out of a log - deliberate, see
dev/changelog/910.

A version string does not say what a build can do, and the questions that cost time are
downstream of it: can this ffmpeg decode HEVC, tonemap HDR, write a JPEG, encode H.264.
describe_capabilities() answers them for exactly the components ChannelBin invokes and no
others (dev/changelog/918). It reports presence in the build - whether the component was
compiled in - which is a different fact from whether a given source will convert cleanly.
"""
import logging
import os
import re
import subprocess
import threading

from .config import (SOURCE_CONFIGURED, SOURCE_PATH,  # noqa: F401 - re-exported, see below
                     SOURCE_SIBLING)

log = logging.getLogger(__name__)

# The source key both tools' standing alerts are addressed by, suffixed per tool. Constant
# for the life of the condition so startup and a later settings save move the same row
# rather than stacking a second one (the standing-alert pattern, app/alerts.py).
ALERT_SOURCE_PREFIX = 'toolchain:'

# `<name> -version` prints its banner and exits immediately; a deadline this generous only
# ever fires for a binary that is hanging rather than one that is merely slow.
_VERSION_TIMEOUT_SECONDS = 15

# The config keys that decide which binaries this module reports on. Every settings write
# path re-probes when one of these moves; naming them once is what stops a new key reaching
# only one of the two hooks in app/routes/settings.py and leaving the card stale until a
# restart.
TOOLCHAIN_CONFIG_KEYS = ('ffmpeg.path', 'ffmpeg.ffprobe_path')

# SOURCE_CONFIGURED / SOURCE_SIBLING / SOURCE_PATH are imported above rather than defined
# here. The vocabulary is the resolvers' own output and lives beside them in app/config.py;
# this module re-exports it because this is where it reaches the API and the Maintenance
# card, and every existing reader spells it `toolchain.SOURCE_*`. A fourth value, `bundled`,
# named the imageio-ffmpeg fallback until dev/changelog/911 removed it - a binary this app
# supplied itself is no longer one of the answers.

_lock = threading.Lock()
_cache = None
_cache_key = None
_capabilities = None
_capabilities_key = None
# {(resolved ffmpeg path, device): True} - the GPU trial encodes that PASSED. A failure is never
# stored: a device that is unplugged, busy or not yet passed into the container costs one
# second to re-ask before the next conversion, where a remembered "no" would keep a GPU
# that has since come back out of use until a restart (dev/changelog/1125).
_gpu_trial = {}

# The source key of the standing GPU alert, suffixed with the configured device.
GPU_ALERT_SOURCE_PREFIX = 'toolchain:gpu:'

# A one-second synthetic clip through the encoder proves the device end to end (the VA
# driver loads, the node opens, the encoder accepts frames). A healthy trial takes well
# under a second; a device that hangs is the only thing this deadline is for.
_GPU_TRIAL_TIMEOUT_SECONDS = 30

# The components of an ffmpeg build that ChannelBin actually invokes, and nothing else - a
# dump of every filter a build carries answers no question an operator has. Each is named by
# what uses it and what breaks without it, because "zscale: missing" alone sends someone off
# to find out whether they care. A decoder or encoder is looked up under its codec, since
# that is how `ffmpeg -codecs` lists them; the one encoder whose name differs from its codec
# (libx264 under h264) is the one most often absent, from LGPL builds.
#
# Adding a component here is the whole change: the probe, the payload and the card all walk
# this tuple. Anything added must be something a spawn in app/ really passes to ffmpeg.
CAPABILITIES = (
    {'kind': 'decoder', 'codec': 'h264', 'name': 'h264', 'label': 'H.264 decoding',
     'used_for': 'Screenshots and live thumbnails of H.264 channels',
     'without': 'H.264 channels get no screenshot or live thumbnail'},
    {'kind': 'decoder', 'codec': 'hevc', 'name': 'hevc', 'label': 'HEVC decoding',
     'used_for': 'Screenshots and live thumbnails of HEVC (H.265) channels',
     'without': 'HEVC channels get no screenshot or live thumbnail'},
    {'kind': 'filter', 'codec': None, 'name': 'zscale', 'label': 'zscale filter',
     'used_for': 'HDR screenshots: converts a PQ or HLG frame to linear light and back',
     'without': 'HDR screenshots are saved without tonemapping'},
    {'kind': 'filter', 'codec': None, 'name': 'tonemap', 'label': 'tonemap filter',
     'used_for': 'HDR screenshots: maps HDR brightness into SDR range',
     'without': 'HDR screenshots are saved without tonemapping'},
    {'kind': 'encoder', 'codec': 'mjpeg', 'name': 'mjpeg', 'label': 'JPEG encoding',
     'used_for': 'Every screenshot and live thumbnail',
     'without': 'No screenshot or live thumbnail can be written'},
    {'kind': 'encoder', 'codec': 'h264', 'name': 'libx264', 'label': 'libx264 encoding',
     'used_for': ('MP4 conversion of a recording whose video is re-encoded '
                  '(recording.post_process.reencode_mode)'),
     'without': 'A recording whose video needs re-encoding fails to convert to MP4'},
    {'kind': 'encoder', 'codec': 'aac', 'name': 'aac', 'label': 'AAC encoding',
     'used_for': 'Audio in every MP4 conversion, including ones that copy the video',
     'without': 'MP4 conversion fails, because its audio is always encoded to AAC'},
)

# "(decoders: h264 h264_qsv libopenh264)" / "(encoders: libx264 libx264rgb)" on a -codecs line.
_CODEC_LIST_RE = re.compile(r'\((decoders|encoders): ([^)]*)\)')


def _probe_version(path):
    """(version, banner_line) for a binary, or (None, None) when it cannot be run.

    subprocess.run's capture_output is drained by communicate() rather than left to fill a
    64KB kernel buffer, which is what CLAUDE.md's subprocess rule actually forbids - and the
    child is reaped before this returns, either normally or by the timeout's kill.

    ffmpeg writes its banner to stderr and ffprobe to stdout depending on build and
    redirection, so both are considered rather than guessed at.
    """
    try:
        proc = subprocess.run([path, '-version'], capture_output=True, text=True,
                              timeout=_VERSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        # OSError covers the missing binary (ENOENT) and the unexecutable one (EACCES);
        # SubprocessError covers the timeout above. Neither is worth a traceback - the
        # caller turns this into a reported state, which is the whole point of the module.
        return None, None
    banner = ''
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or '').splitlines():
            if line.strip():
                banner = line.strip()
                break
        if banner:
            break
    if not banner:
        return None, None
    # "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) ..." -> "6.1.1-3ubuntu5". A static build
    # writes "n7.1.1-..." here and a git build writes a hash; the token is reported as it
    # was printed rather than normalized, because a version this app cannot parse is still
    # the answer to the question and inventing a tidier one would lose it.
    version = None
    parts = banner.split()
    if len(parts) >= 3 and parts[1] == 'version':
        version = parts[2]
    return version, banner


def _absolute(path):
    """`path` as an absolute location on disk when it is a bare name found on PATH.

    Display only, and it deliberately does NOT feed back into what gets spawned:
    resolve_ffmpeg_path returns the configured name unchanged when that name is on PATH,
    which is right for an argv[0] and useless as an answer to "which ffmpeg is this" - the
    question the whole module exists to answer. Left alone when nothing resolves, so a
    missing tool still reports the name that was looked for.
    """
    import shutil
    return shutil.which(path) or path


def _tool_entry(name, config_key, configured_path, resolved, source):
    """One describe_tools() entry, probed. The shape both binaries report in."""
    version, banner = _probe_version(resolved)
    return {
        'name': name,
        'configured': configured_path,
        'config_key': config_key,
        'path': _absolute(resolved),
        'found': version is not None or banner is not None,
        # Reported only for a binary that actually ran: the resolvers return the configured
        # name unchanged when nothing resolves, and calling that 'path' would be this
        # module claiming a provenance it does not have.
        'source': source if banner is not None else None,
        'version': version,
        'banner': banner,
    }


def _describe_ffmpeg(configured_path):
    from .config import resolve_ffmpeg_path
    resolved = resolve_ffmpeg_path(configured_path)
    source = SOURCE_CONFIGURED if os.sep in configured_path else SOURCE_PATH
    return _tool_entry('ffmpeg', 'ffmpeg.path', configured_path, resolved, source)


def _describe_ffprobe(configured_path, configured_ffmpeg_path):
    from .config import describe_ffprobe_resolution
    resolved, source = describe_ffprobe_resolution(configured_path, configured_ffmpeg_path)
    return _tool_entry('ffprobe', 'ffmpeg.ffprobe_path', configured_path, resolved, source)


def describe_tools_uncached(configured_ffmpeg_path='ffmpeg', configured_ffprobe_path=''):
    """Probe both binaries and return {'ffmpeg': {...}, 'ffprobe': {...}}.

    The uncached half, kept separate so tests can drive it with a patched `_probe_version`
    without writing an answer into the process-wide cache that the next test module would
    then read.
    """
    return {
        'ffmpeg': _describe_ffmpeg(configured_ffmpeg_path),
        'ffprobe': _describe_ffprobe(configured_ffprobe_path, configured_ffmpeg_path),
    }


def describe_tools(configured_ffmpeg_path=None, configured_ffprobe_path=None):
    """The cached answer to "which ffmpeg and ffprobe is this install running".

    Cached because finding out costs two process spawns - measured at ~105ms each on this
    machine - and this is read at startup, on every settings save that touches ffmpeg.path,
    and on every load of the Maintenance page. CLAUDE.md's no-hidden-I/O rule covers a stats
    payload rendered on a page exactly as it covers a per-row loop.

    The key is BOTH configured paths, so changing either in Settings re-probes on its own
    without anything having to remember to invalidate. Both halves are load-bearing: keyed
    on ffmpeg alone, setting ffprobe_path would serve the previously-resolved ffprobe's
    version for the life of the process, and keyed on ffprobe alone, moving ffmpeg.path
    would not notice that the sibling rule now resolves somewhere else. What it deliberately
    does not notice is the binary at an unchanged path being replaced underneath the
    process; a restart is what re-reads that, and installing an ffmpeg under a running DVR
    is not a thing this app should be optimizing for.
    """
    global _cache, _cache_key
    if configured_ffmpeg_path is None or configured_ffprobe_path is None:
        from .config import load_config
        ffmpeg_cfg = load_config().get('ffmpeg', {})
        if configured_ffmpeg_path is None:
            configured_ffmpeg_path = ffmpeg_cfg.get('path', 'ffmpeg')
        if configured_ffprobe_path is None:
            configured_ffprobe_path = ffmpeg_cfg.get('ffprobe_path', '')
    key = (configured_ffmpeg_path, configured_ffprobe_path)
    with _lock:
        if _cache is not None and _cache_key == key:
            return _cache
    # Outside the lock: two subprocess spawns is far too long to hold one, and the worst a
    # race can do here is probe twice and agree.
    described = describe_tools_uncached(configured_ffmpeg_path, configured_ffprobe_path)
    with _lock:
        _cache = described
        _cache_key = key
    return described


def reset_cache():
    """Drop every cached probe so the next describe_tools(), describe_capabilities() and
    check_gpu_encoder() spawn again. All of them, because app/probe.py calls this the
    moment a spawn proves a binary has gone, and an answer outliving that would describe a
    build that is no longer there."""
    global _cache, _cache_key, _capabilities, _capabilities_key
    with _lock:
        _cache = None
        _cache_key = None
        _capabilities = None
        _capabilities_key = None
        _gpu_trial.clear()


# ─────────────────────────────────────────────────────────────────────────────
# The GPU encoder: does the configured device really encode?
# ─────────────────────────────────────────────────────────────────────────────
# Two facts, kept apart on purpose (dev/changelog/1125): "this build lists h264_vaapi" is
# what `ffmpeg -codecs` says and is answered by describe_capabilities(); "the device
# encodes" is only ever answered by encoding something through it. A build with the
# encoder compiled in fails all the same when no VA driver is installed, when /dev/dri was
# bind-mounted rather than passed as a device, or when the app's user cannot open the node.

def gpu_trial_cmd(ffmpeg_path, device):
    """The one-second trial encode: a synthetic 320x240 clip uploaded to the device and
    encoded with h264_vaapi to a null muxer. The same `-vaapi_device` / `format=nv12,
    hwupload` / `h264_vaapi` shape the conversion uses, so a pass here means that command
    will open the device too."""
    return [ffmpeg_path, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-vaapi_device', device, '-f', 'lavfi', '-i', 'testsrc2=s=320x240:d=1',
            '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', '-f', 'null', '-']


def _run_gpu_trial(ffmpeg_path, device):
    """(returncode, stderr tail) of one trial encode. The seam tests patch: nothing else in
    this module spawns for the GPU. Same subprocess discipline as _probe_version."""
    try:
        proc = subprocess.run(gpu_trial_cmd(ffmpeg_path, device), capture_output=True,
                              text=True, timeout=_GPU_TRIAL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f'The trial encode produced nothing in {_GPU_TRIAL_TIMEOUT_SECONDS}s'
    except OSError as exc:
        return None, f'Could not run {ffmpeg_path}: {exc}'
    lines = [ln.strip() for ln in (proc.stderr or '').splitlines() if ln.strip()]
    return proc.returncode, '\n'.join(lines[-6:])


class GpuTrial:
    """What check_gpu_encoder() found. `error` is ffmpeg's own last lines when the encode
    failed, already fit to quote in an event or an alert; '' on a pass."""
    def __init__(self, ok, error='', cached=False):
        self.ok = ok
        self.error = error
        self.cached = cached


def check_gpu_encoder(ffmpeg_path, device):
    """Prove the device encodes, and move its standing alert to match.

    A pass is served from cache for the life of the process (keyed on both the binary and
    the device, so a settings change re-asks); a failure is re-run on every call, per the
    note on _gpu_trial. Called before every conversion that would use the GPU, on a
    Readiness ask, on a settings save that turns the GPU on or moves its device, and once at
    startup while the GPU is on (app/readiness.py::refresh_gpu_checks) - a second each,
    against jobs measured in minutes to hours.

    The alert half follows report_tool_state(): one row per device, raised on a failure
    and dismissed on a pass, and never allowed to raise into the caller - the trial is a
    diagnostic, and a failed alert write must not decide whether a conversion runs.

    The cache is keyed on the binary as it resolves on disk, not on the caller's spelling
    of it: the conversion passes the configured `ffmpeg` and Readiness passes the resolved
    `/usr/bin/ffmpeg`, and one binary spelled two ways ran the trial twice per process
    (dev/changelog/1127).

    Every answer, a cached pass included, also becomes Readiness's "The GPU encoder works"
    line, so a conversion's trial and a settings save's trial update the same surface the
    button does rather than leaving it at "Not run yet" beside an alert that says otherwise.
    """
    key = (_absolute(ffmpeg_path), device)
    with _lock:
        cached = bool(_gpu_trial.get(key))
    if cached:
        trial = GpuTrial(True, cached=True)
        _record_in_readiness(device, trial)
        return trial
    rc, tail = _run_gpu_trial(ffmpeg_path, device)
    ok = rc == 0
    if ok:
        with _lock:
            _gpu_trial[key] = True
        log.info('GPU encoder trial passed on %s (%s)', device, key[0])
    else:
        # WARNING, not ERROR: the log->alert handler would raise a second, unlabeled row
        # beside the standing one below.
        log.warning('GPU encoder trial FAILED on %s (%s): %s', device, key[0],
                    tail or f'ffmpeg exited {rc}')
    _report_gpu_state(device, ok, tail or f'ffmpeg exited {rc}')
    trial = GpuTrial(ok, '' if ok else (tail or f'ffmpeg exited {rc}'))
    _record_in_readiness(device, trial)
    return trial


def _record_in_readiness(device, trial):
    from .readiness import record_gpu_trial
    record_gpu_trial(device, trial)


def _report_gpu_state(device, ok, error):
    from flask import has_app_context
    if not has_app_context():
        return
    from .alerts import (GPU_ENCODER_UNAVAILABLE, create_alert, dismiss_open_alerts,
                         has_open_alert)
    source = f'{GPU_ALERT_SOURCE_PREFIX}{device}'
    try:
        if ok:
            dismiss_open_alerts(GPU_ENCODER_UNAVAILABLE, source)
        elif not has_open_alert(GPU_ENCODER_UNAVAILABLE, source):
            create_alert(
                GPU_ENCODER_UNAVAILABLE,
                'The GPU encoder is not working, so conversions are using the CPU',
                body=(f'Settings ask for video re-encodes to run on the GPU '
                      f'(recording.post_process.video_encoder: vaapi), but a one-second '
                      f'test encode through {device} failed. Every conversion that needs a '
                      f're-encode still completes, using libx264 on the CPU, and says so in '
                      f'its event log. ffmpeg reported: {error}\n\n'
                      f'In Docker the device has to be passed as a device (--device '
                      f'/dev/dri, or --device=/dev/dri in the Extra Parameters of the '
                      f'Unraid template), not as a folder mapping, and it is only '
                      f'available on a Linux host. If the node is named differently on '
                      f'this machine, set recording.post_process.vaapi_device. '
                      f'Maintenance > Readiness re-runs the test on demand, and this '
                      f'alert clears itself once it passes.'),
                source=source)
    except Exception:
        log.exception('Could not update the GPU encoder alert')


def _run_listing(path, flag):
    """stdout of `<ffmpeg> -hide_banner <flag>`, or None when it cannot be run.

    One flag per spawn, and that is not a choice: ffmpeg exits after the first listing
    option it handles, so `-filters -codecs` prints the filters and nothing else (measured
    on 7.1.5, in both orders). Same subprocess discipline as _probe_version: drained by
    communicate(), reaped before returning, bounded by the same deadline.
    """
    try:
        proc = subprocess.run([path, '-hide_banner', flag], capture_output=True, text=True,
                              timeout=_VERSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning('Could not list %s for %s: %s', flag, path, exc)
        return None
    if proc.returncode != 0:
        log.warning('%s %s exited %s, so what it includes could not be checked',
                    path, flag, proc.returncode)
        return None
    return proc.stdout


def parse_filter_listing(listing):
    """The filter names in `ffmpeg -filters` output, or None if none could be read.

    A filter line is ` .SC zscale   V->V   Apply resizing...`; the third token carries the
    pad arrow on every filter, sources and sinks included (`|->V`, `V->|`), and on none of
    the legend lines above them, which is what tells the two apart.

    None rather than an empty set when nothing parses: a listing this cannot read is an
    unknown, and reporting every component as absent from it would be a false alarm about
    a build that is probably fine.
    """
    names = set()
    for line in (listing or '').splitlines():
        tokens = line.split()
        if len(tokens) >= 3 and '->' in tokens[2]:
            names.add(tokens[1])
    return names or None


def parse_codec_listing(listing):
    """{codec: {'decoders': set, 'encoders': set}} from `ffmpeg -codecs`, or None.

    A codec line is ` DEV.LS h264   H.264 / AVC ... (decoders: h264 h264_qsv) (encoders:
    libx264 ...)`. ffmpeg prints a parenthesized list only when some implementation's name
    differs from the codec's, and when it prints one the list is complete. So with no list,
    a D or E flag means exactly one implementation named after the codec: `DEA.L. aac ...
    (decoders: aac aac_fixed)` has a native `aac` encoder that appears nowhere by name.

    None when nothing parses, for the same reason as parse_filter_listing.
    """
    codecs = {}
    for line in (listing or '').splitlines():
        tokens = line.split()
        # The legend lines (` D..... = Decoding supported`) share the flag column's shape,
        # and differ only in having '=' where a codec name goes.
        if len(tokens) < 2 or len(tokens[0]) != 6 or tokens[1] == '=':
            continue
        flags, name = tokens[0], tokens[1]
        if not set(flags) <= set('DEVASTIL.'):
            continue
        lists = dict(_CODEC_LIST_RE.findall(line))
        entry = {}
        for kind, flag_index, flag in (('decoders', 0, 'D'), ('encoders', 1, 'E')):
            if kind in lists:
                entry[kind] = set(lists[kind].split())
            else:
                entry[kind] = {name} if flags[flag_index] == flag else set()
        codecs[name] = entry
    return codecs or None


def _component_available(component, filters, codecs):
    """True / False, or None when the listing that would answer it could not be read."""
    if component['kind'] == 'filter':
        return None if filters is None else component['name'] in filters
    if codecs is None:
        return None
    entry = codecs.get(component['codec'])
    return entry is not None and component['name'] in entry[component['kind'] + 's']


def describe_capabilities_uncached(ffmpeg_path):
    """Probe `ffmpeg_path` and return CAPABILITIES in order, each with `available` set.

    The uncached half, kept separate for the same reason as describe_tools_uncached.
    """
    filters = parse_filter_listing(_run_listing(ffmpeg_path, '-filters'))
    codecs = parse_codec_listing(_run_listing(ffmpeg_path, '-codecs'))
    described = []
    for component in CAPABILITIES:
        available = _component_available(component, filters, codecs)
        if available is False:
            log.warning('%s does not include %s (%s): %s', ffmpeg_path, component['name'],
                        component['kind'], component['without'])
        described.append({**component, 'available': available})
    return described


def describe_capabilities(ffmpeg_info):
    """The cached answer to "what can this ffmpeg do", for a describe_tools() ffmpeg entry.

    None when that ffmpeg could not be run - there is no build to describe, and the
    missing-tool alert and card warning already say so.

    Probed on demand rather than inside describe_tools(), which startup, every settings
    save and ffprobe_missing() all read: only the Maintenance card needs this, so only the
    card pays its two spawns (~25ms each on this machine). Keyed on the resolved binary
    rather than on the config pair, because this is a fact about one file on disk - a
    settings change that resolves to the same ffmpeg changes nothing here, and one that
    resolves elsewhere changes the key.
    """
    global _capabilities, _capabilities_key
    if not ffmpeg_info.get('found'):
        return None
    key = ffmpeg_info['path']
    with _lock:
        if _capabilities is not None and _capabilities_key == key:
            return _capabilities
    described = describe_capabilities_uncached(key)
    with _lock:
        _capabilities = described
        _capabilities_key = key
    return described


def missing_tools(tools):
    """The names of the tools in a describe_tools() payload that could not be run."""
    return [name for name in ('ffmpeg', 'ffprobe') if not tools[name]['found']]


def ffprobe_missing(configured_ffmpeg_path=None, configured_ffprobe_path=None) -> bool:
    """Whether ffprobe cannot be run on this machine.

    The predicate a caller holding an empty probe result asks to find out WHICH empty
    result it is holding. parse_ffprobe() returns {} both for "this file has no video
    stream" - a fact about the file, and a perfectly good answer - and for "there is no
    ffprobe here", which is a configuration error. Three surfaces rendered a verdict off
    that ambiguity and picked the wrong one, blaming a provider's stream for ChannelBin's
    own install (dev/changelog/911).

    Free on the warm path: it reads describe_tools()'s process-wide cache, and app/probe.py
    resets that cache the moment a spawn proves the binary has gone, so the answer here is
    never stale in the direction that matters.
    """
    return not describe_tools(configured_ffmpeg_path,
                              configured_ffprobe_path)['ffprobe']['found']


_CONSEQUENCE = {
    'ffmpeg': ('Nothing can be captured or converted without it: every recording and every '
               'health check will fail as soon as it starts.'),
    'ffprobe': ('It ships alongside ffmpeg but is a separate binary. Without it ChannelBin '
                'can still record, but it cannot read a stream or a finished file: no '
                'format detection, no recording health numbers, and no channel-group '
                'format matching.'),
}


def report_tool_state(source: str, configured_ffmpeg_path=None, configured_ffprobe_path=None):
    """Log which tools resolved, and move each one's standing alert to match.

    Modelled on app/auth.py::report_gate_state, and called from the same two kinds of place:
    create_app() at startup, and the settings write paths when ffmpeg.path changes. Both
    halves are load-bearing - a tool that went missing since the last save must be loud
    without waiting for a restart, and one that has been installed since must clear itself
    without waiting for one either.

    `source` names which path observed the state and reaches only the log line. The alert's
    own source key stays constant, so the two paths address one row rather than stacking.

    Never raises. This runs inside create_app(), and a diagnostic that can stop the app
    booting is the opposite of what it was asked for.
    """
    from flask import has_app_context
    from .alerts import (EXTERNAL_TOOL_MISSING, create_alert, dismiss_open_alerts,
                         has_open_alert)

    tools = describe_tools(configured_ffmpeg_path, configured_ffprobe_path)
    missing = missing_tools(tools)
    for name in ('ffmpeg', 'ffprobe'):
        info = tools[name]
        if info['found']:
            log.info('Resolved %s: %s (%s, from %s)',
                     name, info['path'], info['version'] or 'version unknown', info['source'])
        else:
            # WARNING, not ERROR: the log->alert handler in app/__init__.py raises an alert
            # for any ERROR record, which would duplicate the row created just below.
            log.warning(
                'Could not run %s (looked for it at: %s). %s Observed at: %s',
                name, info['path'], _CONSEQUENCE[name], source)

    if not has_app_context():
        return tools
    try:
        for name in ('ffmpeg', 'ffprobe'):
            alert_source = f'{ALERT_SOURCE_PREFIX}{name}'
            if name not in missing:
                dismiss_open_alerts(EXTERNAL_TOOL_MISSING, alert_source)
            elif not has_open_alert(EXTERNAL_TOOL_MISSING, alert_source):
                create_alert(
                    EXTERNAL_TOOL_MISSING,
                    f'{name} is missing, so part of ChannelBin cannot work',
                    body=(f'ChannelBin could not run {name}. It was looked for at '
                          f'"{tools[name]["path"]}". {_CONSEQUENCE[name]} '
                          f'Install ffmpeg (which supplies both ffmpeg and ffprobe) on this '
                          f'machine, or set ffmpeg.path in Settings to the absolute path of '
                          f'an ffmpeg binary - ChannelBin takes ffprobe from the same '
                          f'directory automatically, and ffmpeg.ffprobe_path overrides that '
                          f'for a toolchain whose halves live apart. '
                          f'Maintenance > External tools shows what is resolved right now, '
                          f'and this alert clears itself once {name} can be run.'),
                    source=alert_source)
    except Exception:
        # Same guard as report_gate_state's: never let the diagnostic break the thing it is
        # describing. This runs inside create_app() and inside a settings save, and a failed
        # alert write must not take down startup or turn a successful save into a 500.
        log.exception('Could not update the external-tool alerts')
    return tools
