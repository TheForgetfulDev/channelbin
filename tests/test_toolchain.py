"""Which ffmpeg/ffprobe this install resolved, and whether a missing one is loud.

The app used to answer "which ffmpeg is this" nowhere at all - not at startup, not on a
page, not as an event, not in an alert - while four different answers were live in the
wild (this box 6.1.1, the container 7.1.5, a pip-only install's bundled 7.0.2, or an
absolute path in ffmpeg.path). The ffprobe half was worse: imageio-ffmpeg supplied an
ffmpeg and no ffprobe, so a pip-only install probed nothing and said nothing about it.
dev/changelog/910 is the surface that ends both silences; this file is what holds it.
dev/changelog/911 then removed that fallback outright, so the two binaries now come from
one install or neither does - but they are still reported separately, because a user can
point ffmpeg.path at a binary with no ffprobe beside it.

What each case guards, all of which a careless edit would quietly undo:

  * A missing tool raises a standing alert, one row per binary, and NEVER stops the app
    booting. Both halves are deliberate (dev/changelog/910): the app still boots, and the
    problem is loud in the UI rather than something to dig out of a log.
  * The alert clears itself once the tool can be run, and does not stack a second row
    while the condition persists - the standing-alert pattern, not one row per observation.
  * The probe is cached process-wide. Uncached it costs two process spawns (~105ms each,
    measured on this machine) at every create_app(), on every settings save and on every
    load of the Maintenance page - CLAUDE.md's no-hidden-I/O rule.
  * The cache is keyed on the configured ffmpeg path, so changing ffmpeg.path re-probes
    rather than serving the old binary's version forever.
  * ffprobe_missing() answers for ffprobe alone. It is the predicate the probe-consuming
    surfaces branch on to tell "this file has no video stream" from "there is no ffprobe
    here", and collapsing the two is the defect dev/changelog/911 closed.
  * What the ffmpeg build includes is reported per component with THREE states. A listing
    that could not be read is "could not be checked", never "not in this build" - the
    other call is a false alarm about a working install. It is probed only when the
    Maintenance card asks, never at startup (dev/changelog/918).

Never spawns a real ffmpeg: _probe_version and _run_listing are patched in every case, so
the results describe the code rather than whatever ffmpeg the machine running the suite
happens to have. The three cases that do want the real answer assert only that it is
self-consistent, or that the parsers can read the real listing format at all.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_toolchain
"""
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from app import config as cfgmod
from app import toolchain
from app.alerts import EXTERNAL_TOOL_MISSING
from app.database import Alert
from tests.support.app import _template_db_path, make_test_app
from tests.support.config_sandbox import ConfigSandbox

FFMPEG_BANNER = 'ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers'
FFPROBE_BANNER = 'ffprobe version 6.1.1-3ubuntu5 Copyright (c) 2007-2023 the FFmpeg developers'


def _fake_probe(found=('ffmpeg', 'ffprobe')):
    """A _probe_version stand-in that reports success only for the named tools.

    Keyed on the path's BASENAME, so it answers for a bare 'ffmpeg' and an absolute
    '/opt/ffmpeg-7.1/bin/ffmpeg' alike while still telling the two binaries apart.
    Matching a substring of the whole path cannot: an ffprobe installed beside its
    ffmpeg lives at a path like '/opt/ffmpeg-7.1/bin/ffprobe', whose parent directory
    contains 'ffmpeg', so every found=('ffmpeg',) case reported the missing ffprobe as
    present and the six cases guarding that surface stopped simulating it
    (dev/changelog/913).
    """
    def probe(path):
        name = os.path.basename(path)
        if name in found:
            banner = FFMPEG_BANNER if name == 'ffmpeg' else FFPROBE_BANNER
            return '6.1.1-3ubuntu5', banner
        return None, None
    return probe


# Excerpts of the real `ffmpeg -hide_banner -filters` / `-codecs` output from the 7.1.5 build
# this project targets, legends included - the legend lines are the part a careless parser
# reads as components.
FILTERS_LISTING = """Filters:
  T.. = Timeline support
  .S. = Slice threading
  ..C = Command support
  A = Audio input/output
  V = Video input/output
  N = Dynamic number and/or type of input/output
  | = Source or sink filter
 ... buffer            |->V       Buffer video frames, and make them accessible to the filterchain.
 ... format            V->V       Convert the input video to one of the specified pixel formats.
 ..C scale             V->V       Scale the input video size and/or convert the image format.
 ... setparams         V->V       Force field, or color property for the output video frame.
 .S. tonemap           V->V       Conversion to/from different dynamic ranges.
 .SC zscale            V->V       Apply resizing, colorspace and bit depth conversion.
 ... nullsink          V->|       Do absolutely nothing with the input video.
"""

CODECS_LISTING = """Codecs:
 D..... = Decoding supported
 .E.... = Encoding supported
 ..V... = Video codec
 ..A... = Audio codec
 ..S... = Subtitle codec
 ..D... = Data codec
 ..T... = Attachment codec
 ...I.. = Intra frame-only codec
 ....L. = Lossy compression
 .....S = Lossless compression
 -------
 D.V.L. 4xm                  4X Movie
 .EVIL. a64_multi            Multicolor charset for Commodore 64 (encoders: a64multi)
 DEV.LS h264                 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (decoders: h264 h264_v4l2m2m h264_qsv libopenh264 h264_cuvid) (encoders: libx264 libx264rgb libopenh264 h264_amf h264_nvenc h264_qsv h264_v4l2m2m h264_vaapi h264_vulkan)
 DEV.L. hevc                 H.265 / HEVC (High Efficiency Video Coding) (decoders: hevc hevc_qsv hevc_v4l2m2m hevc_cuvid) (encoders: libx265 hevc_amf hevc_nvenc hevc_qsv hevc_v4l2m2m hevc_vaapi hevc_vulkan libkvazaar)
 DEVIL. mjpeg                Motion JPEG (decoders: mjpeg mjpeg_cuvid mjpeg_qsv) (encoders: mjpeg mjpeg_qsv mjpeg_vaapi)
 DEA.L. aac                  AAC (Advanced Audio Coding) (decoders: aac aac_fixed)
"""


def _without(listing, name, replacement):
    """`listing` with the one line naming `name` swapped for `replacement` ('' drops it)."""
    lines = []
    for line in listing.splitlines():
        tokens = line.split()
        lines.append(replacement if len(tokens) > 1 and tokens[1] == name else line)
    return '\n'.join(lines) + '\n'


def _fake_listing(filters=FILTERS_LISTING, codecs=CODECS_LISTING, calls=None):
    """A _run_listing stand-in answering from the listings above."""
    def run(path, flag):
        if calls is not None:
            calls.append((path, flag))
        return {'-filters': filters, '-codecs': codecs}[flag]
    return run


class FfprobeResolutionTests(unittest.TestCase):
    """Which ffprobe gets spawned, and why - the three-way order dev/changelog/914 added.

    Until then there was one answer, PATH, justified on the grounds that ffprobe ships
    beside ffmpeg in every real install. True of a distro install and false the moment
    ffmpeg.path points at a side-by-side build, which left captures on the configured
    binary while every probe, health number and format lock was measured by whatever PATH
    held - a split toolchain, and the reason dev/changelog/913 had to A/B by prepending
    PATH rather than by using the setting that exists for it.

    Filesystem only: no config, no app, no spawning. The candidate binaries are real files
    on disk because the sibling rule tests them for existence and the executable bit.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.bin = os.path.join(self.tmp, 'ffmpeg-7.1', 'bin')
        os.makedirs(self.bin)
        self.ffmpeg = self._exe(os.path.join(self.bin, 'ffmpeg'))
        self.ffprobe = self._exe(os.path.join(self.bin, 'ffprobe'))

    def _exe(self, path):
        with open(path, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_ffprobe_is_taken_from_beside_a_configured_ffmpeg(self):
        """Point ffmpeg.path at a side-by-side build and the whole toolchain moves, without
        a second field having to be filled in."""
        self.assertEqual(cfgmod.describe_ffprobe_resolution('', self.ffmpeg),
                         (self.ffprobe, cfgmod.SOURCE_SIBLING))

    def test_an_explicit_path_wins_over_the_sibling(self):
        """The override exists for a toolchain whose halves genuinely live apart, so it
        has to beat the rule that would otherwise be right."""
        other = self._exe(os.path.join(self.tmp, 'ffprobe'))
        self.assertEqual(cfgmod.describe_ffprobe_resolution(other, self.ffmpeg),
                         (other, cfgmod.SOURCE_CONFIGURED))

    def test_an_explicit_path_is_returned_even_when_it_does_not_resolve(self):
        """resolve_ffmpeg_path's contract, mirrored deliberately: a configured value that
        cannot be run must still reach the spawn, so app/probe.py's OSError handler names
        the tool and raises the standing alert. Silently falling back to a working PATH
        ffprobe would make a typo look like it took effect (dev/changelog/911)."""
        bogus = os.path.join(self.tmp, 'no', 'such', 'ffprobe')
        self.assertEqual(cfgmod.describe_ffprobe_resolution(bogus, self.ffmpeg),
                         (bogus, cfgmod.SOURCE_CONFIGURED))

    def test_a_configured_ffmpeg_with_no_ffprobe_beside_it_falls_through_to_path(self):
        """The fall-through is what keeps the sibling rule a preference rather than a
        trap: pinning the probe to a path that cannot run would break every measurement
        on an install whose ffprobe is simply somewhere else."""
        os.makedirs(os.path.join(self.tmp, 'lonely'))
        lonely = self._exe(os.path.join(self.tmp, 'lonely', 'ffmpeg'))
        with patch('shutil.which', lambda name: f'/usr/bin/{name}'):
            self.assertEqual(cfgmod.describe_ffprobe_resolution('', lonely),
                             ('/usr/bin/ffprobe', cfgmod.SOURCE_PATH))

    def test_a_sibling_that_is_not_executable_does_not_win(self):
        os.chmod(self.ffprobe, stat.S_IRUSR | stat.S_IWUSR)
        with patch('shutil.which', lambda name: f'/usr/bin/{name}'):
            self.assertEqual(cfgmod.describe_ffprobe_resolution('', self.ffmpeg),
                             ('/usr/bin/ffprobe', cfgmod.SOURCE_PATH))

    def test_a_bare_ffmpeg_name_never_claims_the_sibling_provenance(self):
        """A bare name resolves through PATH to somewhere like /usr/bin, whose ffprobe is
        the one PATH would have found anyway - so reporting it as "beside the configured
        ffmpeg" would be a provenance describing nothing."""
        with patch('shutil.which', lambda name: f'/usr/bin/{name}'):
            self.assertEqual(cfgmod.describe_ffprobe_resolution('', 'ffmpeg'),
                             ('/usr/bin/ffprobe', cfgmod.SOURCE_PATH))

    def test_nothing_resolving_still_returns_the_bare_name(self):
        """So the caller fails with an OS error naming the tool rather than on an empty
        argv[0] - which is what app/probe.py reads as a configuration problem."""
        with patch('shutil.which', lambda name: None):
            self.assertEqual(cfgmod.describe_ffprobe_resolution('', 'ffmpeg'),
                             ('ffprobe', cfgmod.SOURCE_PATH))

    def test_a_leading_tilde_is_expanded_for_every_path_a_user_can_type(self):
        """Nothing else in the stack will: the value reaches subprocess as argv[0] with no
        shell in between, so "~/opt/ffmpeg-7.1/bin/ffmpeg" is an ENOENT rather than a path
        - and a settings field is exactly where someone types one."""
        with patch.dict(os.environ, {'HOME': self.tmp}):
            tilde_ffmpeg = '~/ffmpeg-7.1/bin/ffmpeg'
            self.assertEqual(cfgmod.resolve_ffmpeg_path(tilde_ffmpeg), self.ffmpeg)
            # Expanded on the ffmpeg side, so the sibling rule can find its directory...
            self.assertEqual(cfgmod.describe_ffprobe_resolution('', tilde_ffmpeg),
                             (self.ffprobe, cfgmod.SOURCE_SIBLING))
            # ...and on the ffprobe side, so an explicitly configured one runs too.
            self.assertEqual(
                cfgmod.describe_ffprobe_resolution('~/ffmpeg-7.1/bin/ffprobe', 'ffmpeg'),
                (self.ffprobe, cfgmod.SOURCE_CONFIGURED))

    def test_both_values_are_read_from_config_when_not_supplied(self):
        """app/probe.py's three spawn sites hold no config and are not inside a per-row
        loop, so the resolver reads it - but a caller that does hold one must be able to
        pass it, and both keys have to be picked up rather than only the obvious one."""
        cfg = {'ffmpeg': {'path': self.ffmpeg, 'ffprobe_path': ''}}
        with patch.object(cfgmod, 'load_config', lambda *a, **k: cfg):
            self.assertEqual(cfgmod.resolve_ffprobe_path(), self.ffprobe)
            cfg['ffmpeg']['ffprobe_path'] = '/somewhere/else/ffprobe'
            self.assertEqual(cfgmod.resolve_ffprobe_path(), '/somewhere/else/ffprobe')

    def test_the_path_helper_and_the_describing_one_never_disagree(self):
        """Two entry points over one decision. A second implementation of the order is the
        defect this guards - two answers to "which ffprobe" is a card naming one binary
        while every probe runs another."""
        for probe_cfg, ffmpeg_cfg in (('', self.ffmpeg), ('/x/ffprobe', 'ffmpeg'),
                                      ('', 'ffmpeg')):
            self.assertEqual(cfgmod.resolve_ffprobe_path(probe_cfg, ffmpeg_cfg),
                             cfgmod.describe_ffprobe_resolution(probe_cfg, ffmpeg_cfg)[0])


class DescribeToolsTests(unittest.TestCase):
    """The payload itself - no app, no DB, no alerts."""

    def tearDown(self):
        toolchain.reset_cache()

    def test_both_tools_present_reports_version_path_and_provenance(self):
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertTrue(tools['ffmpeg']['found'])
        self.assertTrue(tools['ffprobe']['found'])
        self.assertEqual(tools['ffmpeg']['version'], '6.1.1-3ubuntu5')
        self.assertEqual(tools['ffmpeg']['banner'], FFMPEG_BANNER)
        self.assertEqual(toolchain.missing_tools(tools), [])

    def test_a_tool_that_cannot_be_run_is_reported_missing_not_omitted(self):
        """The defect this whole module exists for: a pip-only install has ffmpeg and no
        ffprobe, and every probe returns {} with nothing anywhere saying why."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertTrue(tools['ffmpeg']['found'])
        self.assertFalse(tools['ffprobe']['found'])
        self.assertIsNone(tools['ffprobe']['version'])
        # Still carries the path it looked at - "not found" without saying where it looked
        # is half an answer, and the path is what tells an operator what to fix.
        self.assertTrue(tools['ffprobe']['path'])
        self.assertEqual(toolchain.missing_tools(tools), ['ffprobe'])

    def test_a_missing_tool_claims_no_provenance(self):
        """`source` describes where a binary that RAN came from. resolve_ffmpeg_path
        returns the configured name unchanged when nothing resolves, so reporting that as
        'found on PATH' would be the module inventing a fact it does not have."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertIsNone(tools['ffmpeg']['source'])
        self.assertIsNone(tools['ffprobe']['source'])

    def test_a_bare_name_on_path_is_reported_as_the_binary_it_resolves_to(self):
        """"ffmpeg" is a correct argv[0] and a useless answer to "which ffmpeg is this",
        which is the only question this card is for. What gets SPAWNED is unchanged."""
        with patch.object(toolchain, '_probe_version', _fake_probe()), \
             patch('shutil.which', lambda p: f'/usr/bin/{p}' if '/' not in p else p):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertEqual(tools['ffmpeg']['configured'], 'ffmpeg')
        self.assertEqual(tools['ffmpeg']['path'], '/usr/bin/ffmpeg')

    def test_a_missing_tool_still_reports_the_name_it_looked_for(self):
        """Nothing to resolve it to, so the bare name stands - "not found" without saying
        where it looked is half an answer."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())), \
             patch('shutil.which', lambda p: None):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertEqual(tools['ffmpeg']['path'], 'ffmpeg')
        self.assertEqual(tools['ffprobe']['path'], 'ffprobe')

    def test_an_absolute_configured_path_is_reported_as_configured(self):
        with patch.object(toolchain, '_probe_version', _fake_probe()), \
             patch('shutil.which', lambda p: p):
            tools = toolchain.describe_tools_uncached('/opt/ffmpeg-7.1/bin/ffmpeg')
        self.assertEqual(tools['ffmpeg']['source'], toolchain.SOURCE_CONFIGURED)
        self.assertEqual(tools['ffmpeg']['path'], '/opt/ffmpeg-7.1/bin/ffmpeg')

    def test_a_sibling_ffprobe_reports_its_own_provenance(self):
        """The card reports provenance per binary, so the third way of being found needs
        its own value - reporting a sibling as SOURCE_PATH would say the system ffprobe is
        in use while the configured build's is what runs."""
        with patch.object(toolchain, '_probe_version', _fake_probe()), \
             patch.object(cfgmod, 'describe_ffprobe_resolution',
                          lambda *a: ('/opt/ffmpeg-7.1/bin/ffprobe', cfgmod.SOURCE_SIBLING)):
            tools = toolchain.describe_tools_uncached('/opt/ffmpeg-7.1/bin/ffmpeg', '')
        self.assertEqual(tools['ffprobe']['source'], toolchain.SOURCE_SIBLING)
        self.assertEqual(tools['ffprobe']['path'], '/opt/ffmpeg-7.1/bin/ffprobe')

    def test_each_tool_names_the_setting_that_moves_it(self):
        """The two binaries have different config keys, and the card renders "set in
        <key>" from this field. A card that told an operator to edit ffmpeg.path to move
        ffprobe would be sending them to the wrong field."""
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            tools = toolchain.describe_tools_uncached('ffmpeg', '')
        self.assertEqual(tools['ffmpeg']['config_key'], 'ffmpeg.path')
        self.assertEqual(tools['ffprobe']['config_key'], 'ffmpeg.ffprobe_path')
        self.assertEqual(set(toolchain.TOOLCHAIN_CONFIG_KEYS),
                         {t['config_key'] for t in tools.values()})

    def test_ffprobe_missing_is_the_predicate_callers_branch_on(self):
        """The three surfaces that render a verdict off an empty probe ask this rather
        than re-deriving it, and it must answer for ffprobe alone - an ffmpeg that runs
        says nothing about whether its sibling does (dev/changelog/911)."""
        toolchain.reset_cache()
        try:
            with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
                self.assertTrue(toolchain.ffprobe_missing('ffmpeg'))
            toolchain.reset_cache()
            with patch.object(toolchain, '_probe_version', _fake_probe()):
                self.assertFalse(toolchain.ffprobe_missing('ffmpeg'))
        finally:
            toolchain.reset_cache()

    def test_the_missing_ffprobe_cases_still_simulate_a_missing_ffprobe(self):
        """A guard on this file's own fixture, because the fixture is what failed.

        Every case above that proves the missing-ffprobe surface depends on _fake_probe
        telling the two binaries apart. It matched the tool name against the WHOLE
        resolved path, so a side-by-side install - ffmpeg and ffprobe together under a
        directory named for ffmpeg, which is what every static build and every /opt
        layout produces - made 'ffmpeg' match the ffprobe path through its parent
        directory. found=('ffmpeg',) then reported the absent ffprobe as present and six
        cases stopped testing what they claim to (dev/changelog/913). Asserted directly
        so the fixture cannot regress without saying so, rather than only surfacing as
        six confusing failures on whichever machine happens to install ffmpeg that way.
        """
        probe = _fake_probe(found=('ffmpeg',))
        for ffprobe_path in ('ffprobe',
                             '/usr/bin/ffprobe',
                             '/opt/ffmpeg-7.1/bin/ffprobe',
                             '/home/someone/ffmpeg-builds/ffmpeg/bin/ffprobe'):
            self.assertEqual(probe(ffprobe_path), (None, None), ffprobe_path)
        for ffmpeg_path in ('ffmpeg', '/usr/bin/ffmpeg', '/opt/ffmpeg-7.1/bin/ffmpeg'):
            self.assertIsNotNone(probe(ffmpeg_path)[0], ffmpeg_path)

    def test_a_banner_it_cannot_parse_still_counts_as_found(self):
        """A git build prints a hash where a release prints a version. Reporting that as
        "not found" would raise a missing-tool alert for a perfectly working ffmpeg."""
        with patch.object(toolchain, '_probe_version',
                          lambda path: (None, 'ffmpeg version N-1234-gabcdef Copyright')):
            tools = toolchain.describe_tools_uncached('ffmpeg')
        self.assertTrue(tools['ffmpeg']['found'])
        self.assertIsNone(tools['ffmpeg']['version'])
        self.assertEqual(toolchain.missing_tools(tools), [])

    def test_the_real_probe_agrees_with_itself(self):
        """No patch: whatever this machine has, `found` and `banner` must not disagree.
        Deliberately asserts nothing about the version - that differs per environment,
        which is the entire reason this module exists."""
        tools = toolchain.describe_tools_uncached('ffmpeg')
        for info in tools.values():
            self.assertEqual(info['found'], info['banner'] is not None)
            if info['found']:
                self.assertIsNotNone(info['source'])


class ToolProbeCacheTests(unittest.TestCase):
    """Uncached, this is two process spawns per create_app(), per settings save and per
    Maintenance load - ~105ms each, measured on this machine."""

    def setUp(self):
        toolchain.reset_cache()

    def tearDown(self):
        toolchain.reset_cache()

    def test_the_probe_runs_once_and_is_served_from_cache_after(self):
        calls = []

        def probe(path):
            calls.append(path)
            return '6.1.1', FFMPEG_BANNER

        with patch.object(toolchain, '_probe_version', probe):
            first = toolchain.describe_tools('ffmpeg')
            second = toolchain.describe_tools('ffmpeg')
        self.assertEqual(len(calls), 2, 'expected exactly one spawn per binary')
        self.assertIs(first, second)

    def test_changing_the_configured_path_reprobes(self):
        """ffmpeg.path is read at every spawn, so a settings change takes effect live -
        and the reported version has to follow it rather than describing the old binary
        for the life of the process."""
        calls = []

        def probe(path):
            calls.append(path)
            return '6.1.1', FFMPEG_BANNER

        with patch.object(toolchain, '_probe_version', probe), \
             patch('shutil.which', lambda p: p):
            toolchain.describe_tools('ffmpeg')
            before = len(calls)
            toolchain.describe_tools('/opt/ffmpeg-7.1/bin/ffmpeg')
        self.assertGreater(len(calls), before)

    def test_changing_only_the_configured_ffprobe_reprobes(self):
        """The cache is keyed on BOTH paths. Keyed on ffmpeg alone, setting
        ffmpeg.ffprobe_path would serve the previously-resolved ffprobe's version for the
        life of the process - the card and the missing-tool alert both stale, with a save
        that appeared to succeed."""
        calls = []

        def probe(path):
            calls.append(path)
            return '6.1.1', FFMPEG_BANNER

        with patch.object(toolchain, '_probe_version', probe), \
             patch('shutil.which', lambda p: p):
            toolchain.describe_tools('ffmpeg', '')
            before = len(calls)
            toolchain.describe_tools('ffmpeg', '/opt/ffmpeg-7.1/bin/ffprobe')
        self.assertGreater(len(calls), before)
        self.assertIn('/opt/ffmpeg-7.1/bin/ffprobe', calls)

    def test_reset_cache_forces_a_fresh_probe(self):
        calls = []

        def probe(path):
            calls.append(path)
            return '6.1.1', FFMPEG_BANNER

        with patch.object(toolchain, '_probe_version', probe):
            toolchain.describe_tools('ffmpeg')
            toolchain.reset_cache()
            toolchain.describe_tools('ffmpeg')
        self.assertEqual(len(calls), 4)


class ToolAlertTests(unittest.TestCase):
    """A missing tool is loud in the UI, not only in a log (dev/changelog/910)."""

    def setUp(self):
        # Reset on BOTH sides of make_test_app(), and neither is redundant. Before: it
        # reports the toolchain at startup, so a cached answer left by an earlier case
        # would decide this one's alert rows. After: report_tool_state() reads through the
        # cache, so a warm one means the patched _probe_version below is never called and
        # the case silently asserts against this machine's real ffmpeg instead.
        toolchain.reset_cache()
        self.t = make_test_app()
        toolchain.reset_cache()

    def tearDown(self):
        toolchain.reset_cache()
        self.t.cleanup()

    def _open(self):
        return Alert.query.filter_by(alert_type=EXTERNAL_TOOL_MISSING,
                                     dismissed_at=None).all()

    def test_a_missing_tool_raises_an_alert_naming_it(self):
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
            toolchain.report_tool_state(source='test')
        alerts = self._open()
        self.assertEqual(len(alerts), 1)
        self.assertIn('ffprobe', alerts[0].title)
        self.assertEqual(alerts[0].source, f'{toolchain.ALERT_SOURCE_PREFIX}ffprobe')

    def test_the_alert_says_what_breaks_and_what_to_do_about_it(self):
        """CLAUDE.md: a failure path that names the problem without naming the remedy is
        half a fix. The one lever a user has is ffmpeg.path, so the body has to name it."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())):
            toolchain.report_tool_state(source='test')
        bodies = ' '.join(a.body for a in self._open())
        self.assertIn('ffmpeg.path', bodies)
        self.assertIn('Install ffmpeg', bodies)

    def test_each_missing_tool_gets_its_own_row(self):
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())):
            toolchain.report_tool_state(source='test')
        self.assertEqual(sorted(a.source for a in self._open()),
                         [f'{toolchain.ALERT_SOURCE_PREFIX}ffmpeg',
                          f'{toolchain.ALERT_SOURCE_PREFIX}ffprobe'])

    def test_the_alert_stands_rather_than_stacking(self):
        """One row for a condition that persists, not one row per observation - startup
        and every later settings save address the same row."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
            toolchain.report_tool_state(source='startup')
            toolchain.reset_cache()
            toolchain.report_tool_state(source='settings')
            toolchain.reset_cache()
            toolchain.report_tool_state(source='settings')
        self.assertEqual(len(self._open()), 1)

    def test_installing_the_tool_clears_the_alert(self):
        """The clear half. Without it an install that has since been fixed stays accused
        forever, which is the failure mode that makes a standing alert worse than none."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
            toolchain.report_tool_state(source='startup')
        self.assertEqual(len(self._open()), 1)
        toolchain.reset_cache()
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            toolchain.report_tool_state(source='settings')
        self.assertEqual(self._open(), [])

    def test_nothing_is_raised_when_both_tools_are_present(self):
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            toolchain.report_tool_state(source='test')
        self.assertEqual(self._open(), [])

    def test_a_failed_alert_write_never_propagates(self):
        """report_tool_state runs inside create_app() and inside a settings save. A
        diagnostic that can take down the app it is describing is the opposite of the
        ask."""
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())), \
             patch('app.alerts.create_alert', side_effect=RuntimeError('boom')):
            toolchain.report_tool_state(source='test')  # must not raise


class MissingToolDoesNotStopBootTests(unittest.TestCase):
    """A missing tool never stops the app booting (dev/changelog/910)."""

    def test_the_app_builds_and_serves_with_neither_tool_present(self):
        # Build the process-wide schema template FIRST, outside the patch. The template is
        # snapshotted from a real create_app() run, so whatever that run wrote to the DB is
        # inherited by every later test in the process - and create_app() reports the
        # toolchain. Were this the first make_test_app() of the process, the two alert rows
        # below would be baked into every other test's starting database.
        _template_db_path()
        with patch.object(toolchain, '_probe_version', _fake_probe(found=())):
            toolchain.reset_cache()
            t = make_test_app()
            try:
                self.assertEqual(t.app.test_client().get('/maintenance').status_code, 200)
                self.assertEqual(len(Alert.query.filter_by(
                    alert_type=EXTERNAL_TOOL_MISSING, dismissed_at=None).all()), 2)
            finally:
                t.cleanup()
                toolchain.reset_cache()


class SettingsSaveReprobesTests(ConfigSandbox):
    """Both save routes re-probe when either binary path moves.

    This is what keeps the External tools card and the missing-tool alert honest without a
    restart, and there are TWO routes - the per-field save settings.js uses and the bulk
    YAML editor - so a key that reaches only one of them leaves the other's saves silently
    stale. Both ask toolchain.TOOLCHAIN_CONFIG_KEYS rather than comparing a literal, which
    is the only reason adding ffmpeg.ffprobe_path did not have to be remembered twice.

    Runs against a sandboxed temp config.yaml, never the real file.
    """

    def setUp(self):
        super().setUp()
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION})
        toolchain.reset_cache()
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)
        self.addCleanup(toolchain.reset_cache)
        self.client = self.t.app.test_client()

    def test_a_field_save_of_the_ffprobe_path_reprobes(self):
        with patch('app.toolchain.report_tool_state') as report_state:
            resp = self.client.post('/api/settings/field',
                                    json={'path': 'ffmpeg.ffprobe_path',
                                          'value': '/opt/ffmpeg-7.1/bin/ffprobe'})
        self.assertEqual(resp.status_code, 200)
        report_state.assert_called_once()

    def test_the_bulk_yaml_save_of_the_ffprobe_path_reprobes_too(self):
        with patch('app.toolchain.report_tool_state') as report_state:
            resp = self.client.post('/settings', data={
                'config_yaml': f'config_version: {cfgmod.CURRENT_CONFIG_VERSION}\n'
                               'ffmpeg:\n  ffprobe_path: /opt/ffmpeg-7.1/bin/ffprobe\n'})
        self.assertEqual(resp.status_code, 302)
        report_state.assert_called_once()

    def test_a_save_that_moves_neither_binary_does_not_reprobe(self):
        """The other half: re-probing is two process spawns, and settings.js saves per
        field, so firing it on every unrelated save would put them on a keystroke."""
        with patch('app.toolchain.report_tool_state') as report_state:
            self.client.post('/api/settings/field',
                             json={'path': 'display.time_format', 'value': '24h'})
        report_state.assert_not_called()


class CapabilityListingParseTests(unittest.TestCase):
    """The two parsers, against the real listing shapes. A parser that misreads the format
    does not error - it reports a present component as absent, on a card whose whole job is
    to be believed."""

    def test_filter_names_are_read_and_the_legend_is_not(self):
        names = toolchain.parse_filter_listing(FILTERS_LISTING)
        for name in ('zscale', 'tonemap', 'setparams', 'buffer', 'nullsink'):
            self.assertIn(name, names)
        for legend_token in ('=', 'T..', 'A', '|', 'Filters:'):
            self.assertNotIn(legend_token, names)

    def test_an_unreadable_filter_listing_is_unknown_not_empty(self):
        """An empty set would read as "this build has no filters at all" and turn every
        filter row red; None is what lets the card say it could not check."""
        for listing in (None, '', 'Filters:\n  T.. = Timeline support\n', 'something else\n'):
            self.assertIsNone(toolchain.parse_filter_listing(listing), repr(listing))

    def test_a_printed_implementation_list_is_read(self):
        codecs = toolchain.parse_codec_listing(CODECS_LISTING)
        self.assertIn('libx264', codecs['h264']['encoders'])
        self.assertIn('h264', codecs['h264']['decoders'])
        self.assertIn('hevc', codecs['hevc']['decoders'])
        self.assertNotIn('libx264', codecs['hevc']['encoders'])

    def test_an_unprinted_implementation_is_named_after_its_codec(self):
        """ffmpeg prints no encoder list when the only encoder shares the codec's name, so
        the native AAC encoder appears on the aac line only as the E flag. Reading the lists
        alone reports it missing from every build there is."""
        codecs = toolchain.parse_codec_listing(CODECS_LISTING)
        self.assertEqual(codecs['aac']['encoders'], {'aac'})
        self.assertEqual(codecs['4xm']['decoders'], {'4xm'})

    def test_no_flag_means_no_implementation(self):
        """The other half of the rule above: without the E flag there is no encoder, named
        after the codec or otherwise."""
        codecs = toolchain.parse_codec_listing(CODECS_LISTING)
        self.assertEqual(codecs['4xm']['encoders'], set())
        self.assertEqual(codecs['a64_multi']['decoders'], set())

    def test_a_printed_list_is_complete(self):
        """When a list IS printed it names every implementation, so the codec's own name
        is not added to it - a64_multi's one encoder is called a64multi."""
        codecs = toolchain.parse_codec_listing(CODECS_LISTING)
        self.assertEqual(codecs['a64_multi']['encoders'], {'a64multi'})

    def test_the_codec_legend_is_not_read_as_codecs(self):
        codecs = toolchain.parse_codec_listing(CODECS_LISTING)
        self.assertNotIn('=', codecs)
        self.assertEqual(set(codecs), {'4xm', 'a64_multi', 'h264', 'hevc', 'mjpeg', 'aac'})

    def test_an_unreadable_codec_listing_is_unknown_not_empty(self):
        for listing in (None, '', 'Codecs:\n D..... = Decoding supported\n -------\n'):
            self.assertIsNone(toolchain.parse_codec_listing(listing), repr(listing))


class DescribeCapabilitiesTests(unittest.TestCase):
    def _describe(self, **listings):
        with patch.object(toolchain, '_run_listing', _fake_listing(**listings)):
            return {c['name']: c for c in
                    toolchain.describe_capabilities_uncached('/opt/ffmpeg-7.1/bin/ffmpeg')}

    def test_a_full_build_reports_every_component_present(self):
        described = self._describe()
        self.assertEqual({n: c['available'] for n, c in described.items()},
                         {c['name']: True for c in toolchain.CAPABILITIES})

    def test_a_build_without_libx264_reports_exactly_that(self):
        """The LGPL builds' shape: H.264 still decodes, and every other encoder is there,
        but libx264 is not among them."""
        lgpl = _without(CODECS_LISTING, 'h264',
                        ' DEV.LS h264   H.264 / AVC (decoders: h264 h264_qsv) '
                        '(encoders: libopenh264 h264_vaapi)')
        described = self._describe(codecs=lgpl)
        self.assertIs(described['libx264']['available'], False)
        self.assertIs(described['h264']['available'], True)
        self.assertIs(described['aac']['available'], True)

    def test_a_build_without_zscale_reports_exactly_that(self):
        described = self._describe(filters=_without(FILTERS_LISTING, 'zscale', ''))
        self.assertIs(described['zscale']['available'], False)
        self.assertIs(described['tonemap']['available'], True)

    def test_a_codec_absent_from_the_listing_is_not_in_the_build(self):
        described = self._describe(codecs=_without(CODECS_LISTING, 'hevc', ''))
        self.assertIs(described['hevc']['available'], False)

    def test_an_unreadable_listing_is_could_not_be_checked_never_absent(self):
        """Each listing answers only for its own components: a -filters spawn that failed
        leaves the filter rows unknown and says nothing about the codec rows."""
        described = self._describe(filters=None)
        self.assertIsNone(described['zscale']['available'])
        self.assertIsNone(described['tonemap']['available'])
        self.assertIs(described['mjpeg']['available'], True)

        described = self._describe(codecs=None)
        self.assertIsNone(described['libx264']['available'])
        self.assertIs(described['zscale']['available'], True)

    def test_a_listing_that_cannot_be_run_or_fails_is_unknown(self):
        """The real _run_listing, against local stand-ins rather than an ffmpeg: a binary
        that does not exist, and one that exits non-zero."""
        with self.assertLogs(toolchain.log, 'WARNING'):
            self.assertIsNone(toolchain._run_listing('/nonexistent/ffmpeg', '-filters'))
        false_bin = next((p for p in ('/bin/false', '/usr/bin/false') if os.path.exists(p)),
                         None)
        if false_bin is None:
            self.skipTest('no false(1) on this machine')
        with self.assertLogs(toolchain.log, 'WARNING'):
            self.assertIsNone(toolchain._run_listing(false_bin, '-filters'))

    def test_a_missing_component_is_logged_naming_what_breaks(self):
        with self.assertLogs(toolchain.log, 'WARNING') as logs:
            self._describe(filters=_without(FILTERS_LISTING, 'tonemap', ''))
        self.assertTrue(any('tonemap' in line and 'tonemapping' in line
                            for line in logs.output), logs.output)

    def test_every_component_says_what_uses_it_and_what_breaks_without_it(self):
        """The card renders these by name. A component with no `used_for` is a row that
        names a filter and leaves the operator to work out whether they care."""
        for component in toolchain.CAPABILITIES:
            for field in ('label', 'used_for', 'without', 'name'):
                self.assertTrue(component[field], (component['name'], field))
            self.assertIn(component['kind'], ('decoder', 'encoder', 'filter'))
            if component['kind'] != 'filter':
                self.assertTrue(component['codec'], component['name'])

    def test_the_parsers_can_read_this_machines_real_listings(self):
        """No patch. Whatever build is on PATH, every component must come back True or
        False: None here means the parsers no longer understand the listing format that
        ffmpeg really prints, which would turn the whole card to "could not be checked"."""
        import shutil
        ffmpeg = shutil.which('ffmpeg')
        if ffmpeg is None:
            self.skipTest('no ffmpeg on PATH')
        described = {c['name']: c['available']
                     for c in toolchain.describe_capabilities_uncached(ffmpeg)}
        for name, available in described.items():
            self.assertIsNotNone(available, name)
        # In every real build: what this asserts is that parsing found them, not the build.
        self.assertTrue(described['h264'])
        self.assertTrue(described['mjpeg'])


class CapabilityCacheTests(unittest.TestCase):
    FFMPEG = {'name': 'ffmpeg', 'found': True, 'path': '/opt/ffmpeg-7.1/bin/ffmpeg'}

    def setUp(self):
        toolchain.reset_cache()

    def tearDown(self):
        toolchain.reset_cache()

    def test_listed_once_then_served_from_cache(self):
        calls = []
        with patch.object(toolchain, '_run_listing', _fake_listing(calls=calls)):
            first = toolchain.describe_capabilities(self.FFMPEG)
            second = toolchain.describe_capabilities(self.FFMPEG)
        self.assertEqual(sorted(flag for _, flag in calls), ['-codecs', '-filters'])
        self.assertIs(first, second)

    def test_a_different_binary_is_listed_again(self):
        calls = []
        with patch.object(toolchain, '_run_listing', _fake_listing(calls=calls)):
            toolchain.describe_capabilities(self.FFMPEG)
            toolchain.describe_capabilities({**self.FFMPEG, 'path': '/usr/bin/ffmpeg'})
        self.assertEqual(len(calls), 4)
        self.assertIn(('/usr/bin/ffmpeg', '-filters'), calls)

    def test_reset_cache_drops_the_capabilities_too(self):
        """app/probe.py resets the cache when a spawn proves a binary has gone. Keeping
        the capabilities through that would describe a build that is no longer there."""
        calls = []
        with patch.object(toolchain, '_run_listing', _fake_listing(calls=calls)):
            toolchain.describe_capabilities(self.FFMPEG)
            toolchain.reset_cache()
            toolchain.describe_capabilities(self.FFMPEG)
        self.assertEqual(len(calls), 4)

    def test_nothing_is_listed_for_an_ffmpeg_that_cannot_run(self):
        calls = []
        with patch.object(toolchain, '_run_listing', _fake_listing(calls=calls)):
            self.assertIsNone(toolchain.describe_capabilities(
                {**self.FFMPEG, 'found': False}))
        self.assertEqual(calls, [])

    def test_the_version_probe_and_its_readers_never_list_capabilities(self):
        """describe_tools() runs at every create_app(), on settings saves, and under
        ffprobe_missing() on the probe paths. Only the Maintenance card needs the listing,
        so only the card may pay for it."""
        calls = []
        with patch.object(toolchain, '_probe_version', _fake_probe()), \
             patch.object(toolchain, '_run_listing', _fake_listing(calls=calls)):
            toolchain.describe_tools('ffmpeg', '')
            toolchain.ffprobe_missing('ffmpeg', '')
            toolchain.report_tool_state('a test', 'ffmpeg', '')
        self.assertEqual(calls, [])


class ToolsEndpointTests(unittest.TestCase):
    def setUp(self):
        toolchain.reset_cache()  # see ToolAlertTests.setUp for why it is reset on both edges
        self.t = make_test_app()
        toolchain.reset_cache()
        self.client = self.t.app.test_client()
        listing = patch.object(toolchain, '_run_listing', _fake_listing())
        listing.start()
        self.addCleanup(listing.stop)

    def tearDown(self):
        toolchain.reset_cache()
        self.t.cleanup()

    def test_the_endpoint_reports_both_tools_and_what_is_missing(self):
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffmpeg',))):
            payload = self.client.get('/api/system/tools').get_json()
        self.assertEqual(payload['missing'], ['ffprobe'])
        self.assertEqual(set(payload['tools']), {'ffmpeg', 'ffprobe'})
        self.assertEqual(payload['tools']['ffmpeg']['version'], '6.1.1-3ubuntu5')

    def test_the_endpoint_carries_every_field_the_card_renders(self):
        """The page reads these by name, so a rename here is a card that renders blanks
        with no error anywhere - the class of defect the storage payload case guards too."""
        with patch.object(toolchain, '_probe_version', _fake_probe()):
            payload = self.client.get('/api/system/tools').get_json()
        for info in payload['tools'].values():
            for field in ('name', 'path', 'found', 'source', 'version', 'config_key'):
                self.assertIn(field, info)
        self.assertEqual([c['name'] for c in payload['capabilities']],
                         [c['name'] for c in toolchain.CAPABILITIES])
        for component in payload['capabilities']:
            for field in ('label', 'used_for', 'without', 'available'):
                self.assertIn(field, component)

    def test_no_capabilities_are_reported_for_an_ffmpeg_that_cannot_run(self):
        with patch.object(toolchain, '_probe_version', _fake_probe(found=('ffprobe',))):
            payload = self.client.get('/api/system/tools').get_json()
        self.assertIsNone(payload['capabilities'])

    def test_the_version_probe_is_not_on_the_polled_stats_endpoint(self):
        """/api/system/stats is polled every 15s by every open tab. Answering "which
        ffmpeg" there would be writing "spawn two processes on a timer" into the design."""
        stats = self.client.get('/api/system/stats').get_json()
        self.assertNotIn('tools', stats)
        self.assertNotIn('capabilities', stats)


if __name__ == '__main__':
    unittest.main()
