"""The content library: the loops every stream is cut from, rendered once by ffmpeg and cached.

A profile is a render spec: picture, size, frame rate, codecs, bitrates and loop length. Each
loop is rendered so that its last frame is followed seamlessly by its first:

- its length is a whole number of video frames and of audio frames (59.94 and 29.97 fps with
  1,024-sample AAC at 48 kHz align every 21.354 s, so most loops are 64.064 s, three periods),
  and a whole number of keyframe intervals, so the keyframe cadence carries across the seam;
- the audio encoder's priming frame, and any frame its padding adds at the end, are dropped with
  a copy remux, after which audio spans exactly the loop;
- the length is recorded at render time, never read back from the file.

Rendering is single-threaded per profile (x264 is not byte-deterministic multi-threaded), with
profiles rendered side by side, so a loop is byte-reproducible on one ffmpeg build. Each file is
keyed by its render spec and the ffmpeg version, with a manifest beside it holding its sha256
and loop length. The cache lives in the per-user cache directory unless `SIM_CONTENT_DIR` (or a
directory handed to `Library`) says otherwise, and a build that would take it past its disk
budget is refused with the numbers rather than started.

Measured in DESIGN-simulator.md §9.1 and §9.5.
"""
import concurrent.futures
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from fractions import Fraction

from .mp3 import SAMPLES_PER_FRAME, Mp3Loop
from .ts import PTS_HZ, Loop

try:
    import fcntl
except ImportError:     # not on Windows; two renders of one profile then race, harmlessly
    fcntl = None

DEFAULT_BUDGET_BYTES = 2 * 1024 ** 3
# Raised whenever the steps after the first render pass change what a loop holds.
FINISH_VERSION = 1
RENDER_TIMEOUT_SECONDS = 3600
AUDIO_FRAME = {'aac': 1024, 'ac3': 1536, 'eac3': 1536, 'mp2': 1152, 'mp3': SAMPLES_PER_FRAME}
_FONTS = ('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',
          '/usr/share/fonts/TTF/DejaVuSansMono.ttf',
          '/usr/share/fonts/dejavu/DejaVuSansMono.ttf',
          '/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf',
          '/Library/Fonts/Courier New.ttf')


class ContentError(RuntimeError):
    """A loop that cannot be rendered, found or loaded, with the reason in words."""


@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    container: str = 'ts'       # ts, or mp3 for a radio mount
    width: int = 0
    height: int = 0
    rate: str = ''              # video frame rate as a fraction, '60000/1001'
    frames: int = 0             # video frames per loop (audio frames per loop for mp3)
    interlaced: bool = False
    video: str = ''             # h264, hevc or mpeg2
    video_kbps: int = 0
    gop: int = 0
    picture: str = 'motion'     # motion, black, frozen or uniform
    hdr: bool = False
    cbr: bool = False           # pad to the bitrate even where the picture needs less
    audio: str = 'aac'          # aac, ac3, eac3, mp2 or mp3
    audio_kbps: int = 128
    channels: int = 2
    sample_rate: int = 48000

    @property
    def seconds(self) -> Fraction:
        if self.container == 'mp3':
            return Fraction(self.frames * SAMPLES_PER_FRAME, self.sample_rate)
        return self.frames / Fraction(self.rate)

    @property
    def samples(self) -> int:
        return int(self.seconds * self.sample_rate)

    @property
    def length90(self) -> int | None:
        if self.container == 'mp3':
            return None
        return int(self.seconds * PTS_HZ)

    @property
    def kbps(self) -> int:
        return self.video_kbps + self.audio_kbps


def _p(name, summary, **kw) -> Profile:
    return Profile(name, summary, **kw)


_HD = dict(width=1920, height=1080)
_720 = dict(width=1280, height=720)
_5994 = dict(rate='60000/1001', frames=3840, gop=120)      # 64.064 s, a keyframe each 2.002 s
_2997 = dict(rate='30000/1001', frames=1920, gop=60)

PROFILES = {p.name: p for p in (
    # The bitrates are the measured medians for each resolution (DESIGN-simulator.md §6.1).
    _p('h264_720p5994_aac', '720p59.94 H.264, stereo AAC - the prod median', **_720, **_5994,
       video='h264', video_kbps=3600),
    _p('h264_720p2997_aac', '720p29.97 H.264, stereo AAC', **_720, **_2997, video='h264',
       video_kbps=3600),
    _p('h264_1080p5994_aac', '1080p59.94 H.264, stereo AAC', **_HD, **_5994, video='h264',
       video_kbps=5000),
    _p('h264_1080p2997_aac', '1080p29.97 H.264, stereo AAC', **_HD, **_2997, video='h264',
       video_kbps=4500),
    _p('h264_1080i2997_aac', '1080i29.97 H.264, stereo AAC', **_HD, **_2997, interlaced=True,
       video='h264', video_kbps=5000),
    _p('h264_576p25_aac', '576p25 H.264, stereo AAC - SD', width=720, height=576, rate='25',
       frames=1600, gop=50, video='h264', video_kbps=1600),
    _p('h264_1080p5994_aac_51', '1080p59.94 H.264, AAC 5.1', **_HD, **_5994, video='h264',
       video_kbps=5000, audio_kbps=384, channels=6),
    _p('h264_1080p5994_ac3_51', '1080p59.94 H.264, AC-3 5.1 (dev/changelog/1163)', **_HD,
       **_5994, video='h264', video_kbps=5000, audio='ac3', audio_kbps=448, channels=6),
    _p('h264_1080p5994_eac3_51', '1080p59.94 H.264, E-AC-3 5.1 (dev/changelog/1163)', **_HD,
       **_5994, video='h264', video_kbps=5000, audio='eac3', audio_kbps=384, channels=6),
    _p('hevc_1080p5994_hdr10', '1080p59.94 HEVC 10-bit HDR10 (dev/changelog/915)', **_HD,
       **_5994, video='hevc', video_kbps=6000, hdr=True),
    # One 21.354 s period rather than three: at four times 1080p's pixels the render costs
    # about 15 minutes at the full length (DESIGN-simulator.md §9.5).
    _p('hevc_2160p5994_hdr10', '2160p59.94 HEVC 10-bit HDR10', width=3840, height=2160,
       rate='60000/1001', frames=1280, gop=128, video='hevc', video_kbps=12000, hdr=True),
    # A closed MPEG-2 GOP with two B-frames comes out 3n+1 frames long whatever -g asks for
    # (-g 12 gives 10), so the GOP is 10: 0.4 s, and 72 s is whole GOPs and MP2 frames.
    _p('mpeg2_576i25_mp2', '576i25 MPEG-2, MP2 - legacy SD', width=720, height=576, rate='25',
       frames=1800, gop=10, interlaced=True, video='mpeg2', video_kbps=3000, audio='mp2',
       audio_kbps=192),
    _p('radio_mp3', 'an audio-only radio mount, MP3 128 kb/s', container='mp3', frames=2450,
       audio='mp3', audio_kbps=128, sample_rate=44100),
    # The finite offline clip real upstreams serve for a down channel, at its measured rate
    # (dev/changelog/957); how long it is served for is the stream fault's business.
    _p('placeholder_1080p30', 'the offline placeholder: black 1080p30 at about 193 kb/s',
       **_HD, rate='30', frames=1920, gop=60, video='h264', video_kbps=125, picture='black',
       audio_kbps=96, cbr=True),
    _p('black_1080p5994', 'a black 1080p59.94 picture with a tone', **_HD, **_5994,
       video='h264', video_kbps=5000, picture='black'),
    _p('frozen_1080p5994', 'one still frame held at 1080p59.94, with a tone', **_HD, **_5994,
       video='h264', video_kbps=5000, picture='frozen'),
    _p('uniform_1080p5994', 'a solid grey 1080p59.94 picture, with a tone', **_HD, **_5994,
       video='h264', video_kbps=5000, picture='uniform'),
    # Under the health check's 1,000 kb/s floor for 720p, for the low-bitrate stream fault.
    _p('h264_720p2997_lowrate', '720p29.97 H.264 at a low rate for its size, stereo AAC', **_720,
       **_2997, video='h264', video_kbps=500, audio_kbps=96),
    # Small and quick to render, for the test suite: one 21.354 s period.
    _p('test_tiny', '180p29.97 H.264, stereo AAC - for tests', width=320, height=180,
       rate='30000/1001', frames=640, gop=64, video='h264', video_kbps=300, audio_kbps=64),
    _p('test_tiny_low', '108p29.97 H.264, stereo AAC - for tests', width=192, height=108,
       rate='30000/1001', frames=640, gop=64, video='h264', video_kbps=150, audio_kbps=48),
    # test_tiny changed one way each, for the stream faults' tests. AC-3's 1,536-sample frames
    # align with 29.97 fps only every 64.064 s, so that one is three periods long.
    _p('test_tiny_5994', '180p59.94 H.264, stereo AAC - for tests', width=320, height=180,
       rate='60000/1001', frames=1280, gop=128, video='h264', video_kbps=300, audio_kbps=64),
    _p('test_tiny_ac3_51', '180p29.97 H.264, AC-3 5.1 - for tests', width=320, height=180,
       rate='30000/1001', frames=1920, gop=64, video='h264', video_kbps=300, audio='ac3',
       audio_kbps=192, channels=6),
    _p('test_tiny_black', '180p29.97 black H.264, stereo AAC - for tests', width=320,
       height=180, rate='30000/1001', frames=640, gop=64, video='h264', video_kbps=300,
       picture='black', audio_kbps=64),
    _p('test_tiny_lowrate', '180p29.97 H.264 at a sixth of test_tiny\'s rate - for tests',
       width=320, height=180, rate='30000/1001', frames=640, gop=64, video='h264',
       video_kbps=50, audio_kbps=32),
)}

TS_PROFILES = tuple(n for n, p in PROFILES.items() if p.container == 'ts')
RADIO_PROFILE = 'radio_mp3'


def check_profiles() -> list[str]:
    """Every profile must loop seamlessly: whole video and audio frames, whole keyframe
    intervals, and a length in whole 90 kHz ticks."""
    problems = []
    for p in PROFILES.values():
        if p.container == 'mp3':
            continue
        size = AUDIO_FRAME[p.audio]
        if (p.seconds * p.sample_rate).denominator != 1 or p.samples % size:
            problems.append(f'{p.name}: {p.seconds} s is not a whole number of {p.audio} frames')
        if p.frames % p.gop:
            problems.append(f'{p.name}: {p.frames} frames is not a whole number of GOPs')
        if (p.seconds * PTS_HZ).denominator != 1:
            problems.append(f'{p.name}: the loop is not a whole number of 90 kHz ticks')
        if p.frames % 4:
            problems.append(f'{p.name}: {p.frames} frames do not split into four scenes')
    return problems


# --- which loop a channel serves --------------------------------------------------------------

_FORMAT_RX = re.compile(r'^(?P<res>\d+)p(?P<fps>[\d.]+)/(?P<video>\w+)/(?P<audio>\w+)/(?P<ch>\d+)$')


def profile_for_format(fmt: str | None) -> str:
    """The library loop nearest a channel's measured format, '1080p59.94/h264/aac/2'.

    The nearest, not an exact match: 50 and 60 fps play at 59.94, 25 and 30 at 29.97, H.264
    above 1080p at 1080p, 5.1 at 1080p, and anything at 576p or below as SD. Rendering every
    combination the profile holds would be about a dozen more loops at 40 to 80 s each, for
    frame rates that are mostly the dev instance's (DESIGN-simulator.md §6.1)."""
    if fmt and fmt.startswith('audio/'):
        return RADIO_PROFILE
    m = _FORMAT_RX.match(fmt or '')
    if not m:
        return 'h264_720p5994_aac'
    res, fps = int(m['res']), float(m['fps'])
    video, audio, channels = m['video'], m['audio'], int(m['ch'])
    high = fps > 40
    if video == 'hevc':
        return 'hevc_2160p5994_hdr10' if res >= 2160 else 'hevc_1080p5994_hdr10'
    if audio == 'ac3':
        return 'h264_1080p5994_ac3_51'
    if audio == 'eac3':
        return 'h264_1080p5994_eac3_51'
    if channels > 2:
        return 'h264_1080p5994_aac_51'
    if res <= 576:
        return 'h264_576p25_aac'
    if res >= 1080:
        return 'h264_1080p5994_aac' if high else 'h264_1080p2997_aac'
    return 'h264_720p5994_aac' if high else 'h264_720p2997_aac'


# --- rendering ------------------------------------------------------------------------------

def find_font() -> str | None:
    for path in _FONTS:
        if os.path.isfile(path):
            return path
    return None


def _label(p: Profile) -> str:
    scan = 'i' if p.interlaced else 'p'
    fps = Fraction(p.rate)
    fps = f'{float(fps):.2f}'.rstrip('0').rstrip('.')
    return f'{p.name.upper().replace("_", " ")} {p.height}{scan}{fps}'


def _video_graph(p: Profile, font: str | None) -> str:
    size, rate = f'{p.width}x{p.height}', p.rate
    if p.picture == 'black':
        chain = f'color=c=black:s={size}:r={rate}'
    elif p.picture == 'uniform':
        chain = f'color=c=0x808080:s={size}:r={rate}'
    elif p.picture == 'frozen':
        chain = (f'testsrc2=s={size}:r={rate},trim=end_frame=1,'
                 f'tpad=stop={p.frames}:stop_mode=clone')
    else:
        # Four scenes of different hue and grain: a real scene change every quarter of the
        # loop, and enough entropy to fill the bitrate the way a channel does.
        quarter = p.frames // 4
        scenes = []
        for i, hue in enumerate((0, 90, 180, 270)):
            scenes.append(f'testsrc2=s={size}:r={rate},trim=end_frame={quarter},hue=h={hue},'
                          f'noise=alls={8 + 4 * i}:allf=t[s{i}]')
        chain = ';'.join(scenes) + ';' + ''.join(f'[s{i}]' for i in range(4))
        chain += 'concat=n=4:v=1:a=0'
        if font:
            chain += (f",drawtext=fontfile={font}:text='{_label(p)} %{{pts\\:hms}}':x=24:y=24:"
                      f"fontsize={max(10, p.height // 18)}:fontcolor=white:box=1:"
                      f"boxcolor=black@0.6")
    if p.interlaced:
        chain += ',setfield=tff'
    chain += ',format=yuv420p10le' if p.hdr else ',format=yuv420p'
    return chain + '[v]'


def _audio_graph(p: Profile) -> str:
    src = (f'anullsrc=r={p.sample_rate}:cl=mono' if p.name.startswith('placeholder')
           else f'sine=f={330 if p.container == "mp3" else 440}:r={p.sample_rate}')
    if p.channels == 6:
        layout = 'pan=5.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0'
    else:
        layout = 'aformat=channel_layouts=stereo'
    return f'{src},{layout},atrim=end_sample={p.samples}[a]'


def _video_codec(p: Profile) -> list:
    k = p.video_kbps
    if p.video == 'h264':
        out = ['-c:v', 'libx264', '-threads', '1', '-preset', 'veryfast', '-profile:v', 'high',
               '-b:v', f'{k}k', '-maxrate', f'{k}k', '-bufsize', f'{2 * k}k',
               '-g', str(p.gop), '-keyint_min', str(p.gop), '-sc_threshold', '0', '-bf', '3']
        params = []
        if p.interlaced:
            out += ['-flags', '+ilme+ildct']
            params.append('tff=1')
        if p.cbr:
            # A black picture compresses to nothing; filler keeps the stream at its rate.
            out += ['-minrate', f'{k}k']
            params.append('nal-hrd=cbr')
        if params:
            out += ['-x264-params', ':'.join(params)]
        return out
    if p.video == 'hevc':
        params = (f'keyint={p.gop}:min-keyint={p.gop}:scenecut=0:open-gop=0:pools=none:'
                  'frame-threads=1:log-level=error')
        if p.hdr:
            params += (':colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:hdr10=1:'
                       'repeat-headers=1')
        out = ['-c:v', 'libx265', '-preset', 'ultrafast', '-b:v', f'{k}k', '-x265-params', params]
        if p.hdr:
            out += ['-color_primaries', 'bt2020', '-color_trc', 'smpte2084',
                    '-colorspace', 'bt2020nc']
        return out
    if p.video == 'mpeg2':
        out = ['-c:v', 'mpeg2video', '-threads', '1', '-b:v', f'{k}k', '-maxrate', f'{k}k',
               '-bufsize', f'{k * 6 // 10}k', '-g', str(p.gop), '-bf', '2',
               # A closed GOP refuses scene-change detection; the GOP is fixed anyway.
               '-sc_threshold', '1000000000']
        out += ['-flags', '+ilme+ildct+cgop'] if p.interlaced else ['-flags', '+cgop']
        return out
    raise ContentError(f'{p.name}: unknown video codec {p.video!r}')


def _audio_codec(p: Profile) -> list:
    codec = {'aac': 'aac', 'ac3': 'ac3', 'eac3': 'eac3', 'mp2': 'mp2', 'mp3': 'libmp3lame'}
    return ['-c:a', codec[p.audio], '-b:a', f'{p.audio_kbps}k']


def render_argv(p: Profile, out: str, font: str | None) -> list:
    """Everything after the ffmpeg binary for the first render pass."""
    argv = ['-hide_banner', '-nostdin', '-y', '-filter_threads', '1',
            '-filter_complex_threads', '1']
    if p.container == 'mp3':
        argv += ['-filter_complex', _audio_graph(p), '-map', '[a]', *_audio_codec(p),
                 '-write_xing', '0', '-id3v2_version', '0', '-f', 'mp3', out]
        return argv
    graph = _video_graph(p, font) + ';' + _audio_graph(p)
    argv += ['-filter_complex', graph, '-map', '[v]', '-map', '[a]', '-frames:v', str(p.frames),
             *_video_codec(p), *_audio_codec(p), '-f', 'mpegts', out]
    return argv


def default_dir() -> str:
    env = os.environ.get('SIM_CONTENT_DIR')
    if env:
        return env
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'channelbin-sim', 'content')


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _tail(path, lines=12) -> str:
    try:
        with open(path, 'r', errors='replace') as f:
            return ''.join(f.readlines()[-lines:]).strip()
    except OSError:
        return ''


# Loops parsed in this process, by file and content: parsing a 60 MB loop takes about a
# second, and every simulator in the process serving it shares one copy. Parsing holds the
# lock: a parse peaks near three copies of the file, and fifty connections arriving at once
# each parsing their own copy measured 4.3 GB before the box's OOM guard killed the process.
_LOADED: dict = {}
_LOADED_LOCK = threading.Lock()


@dataclass
class BuildResult:
    name: str
    path: str
    rendered: bool              # False when the cache already held it
    seconds: float = 0.0
    bytes: int = 0


class Library:
    """The cache of rendered loops. `ffmpeg` is the binary to render with: the argument, else
    `SIM_FFMPEG`, else `ffmpeg` on PATH. Serving needs none - a cache rendered elsewhere is read
    as it is."""

    def __init__(self, directory: str | None = None, ffmpeg: str | None = None,
                 budget_bytes: int = DEFAULT_BUDGET_BYTES):
        self.directory = os.path.abspath(directory or default_dir())
        self._ffmpeg = ffmpeg
        self.budget_bytes = budget_bytes
        self._version = None
        self._font = find_font()
        self._font_digest = _sha256(self._font)[:16] if self._font else None

    # --- the tools --------------------------------------------------------------------------

    @property
    def ffmpeg(self) -> str | None:
        return self._ffmpeg or os.environ.get('SIM_FFMPEG') or shutil.which('ffmpeg')

    @property
    def ffprobe(self) -> str | None:
        ff = self.ffmpeg
        if ff:
            sibling = os.path.join(os.path.dirname(ff), 'ffprobe')
            if os.path.isfile(sibling):
                return sibling
        return shutil.which('ffprobe')

    def ffmpeg_version(self) -> str | None:
        if self._version is None and self.ffmpeg:
            out = subprocess.run([self.ffmpeg, '-hide_banner', '-version'], capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, timeout=60)
            self._version = (out.stdout.splitlines() or [''])[0].strip()
        return self._version

    # --- keys and lookups -------------------------------------------------------------------

    def spec_key(self, name: str) -> str:
        """A digest of what decides a render's bytes - its ffmpeg argv, the font it draws with
        and the finishing rules - and nothing else, so a profile's summary or a new field
        with no effect on the picture never turns a good render into a stale one."""
        p = _profile(name)
        spec = {'argv': render_argv(p, 'OUT', 'FONT' if self._font_tag(p) else None),
                'font': self._font_digest if self._font_tag(p) else None,
                'finish': FINISH_VERSION}
        return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]

    def _font_tag(self, p: Profile) -> str | None:
        # The font is part of the picture, so the spec key carries the font file's digest.
        if p.container != 'ts' or p.picture != 'motion' or not self._font:
            return None
        return self._font

    def _build_key(self) -> str | None:
        version = self.ffmpeg_version()
        if version is None:
            return None
        return hashlib.sha256(version.encode()).hexdigest()[:8]

    def _manifests(self, name: str) -> list[dict]:
        key = self.spec_key(name)
        out = []
        try:
            files = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        for f in files:
            if f.startswith(f'{name}-{key}-') and f.endswith('.json'):
                try:
                    with open(os.path.join(self.directory, f), encoding='utf-8') as fh:
                        m = json.load(fh)
                except (OSError, ValueError):
                    continue
                if os.path.isfile(os.path.join(self.directory, m.get('file', ''))):
                    out.append(m)
        return sorted(out, key=lambda m: m.get('rendered_at', ''), reverse=True)

    def find(self, name: str) -> dict | None:
        """The manifest of the render to serve: this ffmpeg's if there is one, else the newest
        render of the same spec from any build, else None."""
        found = self._manifests(name)
        if not found:
            return None
        build = self._build_key() if self.ffmpeg else None
        exact = [m for m in found if m.get('build') == build]
        return (exact or found)[0]

    def has(self, name: str) -> bool:
        return self.find(name) is not None

    def path(self, name: str) -> str | None:
        m = self.find(name)
        return os.path.join(self.directory, m['file']) if m else None

    def entries(self) -> list[dict]:
        """Every manifest in the cache, current or not."""
        out = []
        try:
            files = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []
        for f in files:
            if f.endswith('.json') and not f.startswith('.'):
                try:
                    with open(os.path.join(self.directory, f), encoding='utf-8') as fh:
                        m = json.load(fh)
                except (OSError, ValueError):
                    continue
                m['current'] = (m.get('profile') in PROFILES
                                and m.get('spec') == self.spec_key(m['profile']))
                out.append(m)
        return out

    def size(self) -> int:
        total = 0
        for m in self.entries():
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(self.directory, m['file']))
        return total

    def prune(self) -> list[str]:
        """Delete every render whose spec no profile has any more, and every render of a spec
        that has a newer one from this ffmpeg. Returns the files removed."""
        removed = []
        keep = {}
        for m in self.entries():
            if m['current']:
                best = self.find(m['profile'])
                keep[m['profile']] = best['file'] if best else None
        for m in self.entries():
            if m['current'] and keep.get(m['profile']) == m['file']:
                continue
            for f in (m['file'], m['file'].rsplit('.', 1)[0] + '.json'):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(self.directory, f))
                    removed.append(f)
        return removed

    # --- loading ----------------------------------------------------------------------------

    def load(self, name: str):
        """The parsed loop for `name` - a `ts.Loop` or an `mp3.Mp3Loop` - or ContentError
        naming the command that renders it."""
        m = self.find(name)
        if m is None:
            raise ContentError(f'loop {name!r} is not rendered in {self.directory}; run '
                               f'`python3 -m sim content build --profile {name}`')
        path = os.path.join(self.directory, m['file'])
        key = (path, m['sha256'])
        with _LOADED_LOCK:
            hit = _LOADED.get(key)
            if hit is not None:
                return hit
            with open(path, 'rb') as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest() != m['sha256']:
                raise ContentError(f'{path} does not match its manifest; delete it and build '
                                   'again')
            if m['container'] == 'mp3':
                loop = Mp3Loop.parse(data, name)
            else:
                loop = Loop(data, m['length90'], name)
            _LOADED[key] = loop
            return loop

    # --- building ---------------------------------------------------------------------------

    def build(self, names, jobs: int | None = None, log=None) -> list[BuildResult]:
        """Render every profile in `names` this ffmpeg has not rendered yet, one thread each and
        up to `jobs` at once (default: one per core)."""
        names = list(dict.fromkeys(names))
        for n in names:
            _profile(n)
        if not self.ffmpeg or not self.ffprobe:
            raise ContentError('rendering needs ffmpeg and ffprobe: put them on PATH or set '
                               'SIM_FFMPEG')
        os.makedirs(self.directory, exist_ok=True)
        todo = [n for n in names if not self._exact(n)]
        results = [BuildResult(n, self.path(n), False) for n in names if n not in todo]
        if not todo:
            return results
        need = sum(_estimate(PROFILES[n]) for n in todo)
        have = self.size()
        if have + need > self.budget_bytes:
            raise ContentError(
                f'rendering {", ".join(todo)} needs about {need / 1e6:.0f} MB; the cache at '
                f'{self.directory} holds {have / 1e6:.0f} MB of a {self.budget_bytes / 1e6:.0f} '
                'MB budget. Prune it, raise the budget, or point SIM_CONTENT_DIR elsewhere.')
        jobs = max(1, min(jobs or os.cpu_count() or 1, len(todo)))
        with concurrent.futures.ThreadPoolExecutor(jobs, thread_name_prefix='sim-render') as ex:
            futures = {ex.submit(self._render_locked, n, log): n for n in todo}
            errors = []
            for fut in concurrent.futures.as_completed(futures):
                try:
                    results.append(fut.result())
                except ContentError as exc:
                    errors.append(str(exc))
        if errors:
            raise ContentError('; '.join(errors))
        return results

    def _exact(self, name: str) -> dict | None:
        build = self._build_key()
        return next((m for m in self._manifests(name) if m.get('build') == build), None)

    def _render_locked(self, name, log) -> BuildResult:
        lock_path = os.path.join(self.directory, f'.{name}-{self.spec_key(name)}.lock')
        with open(lock_path, 'w') as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                done = self._exact(name)
                if done:
                    # Another process rendered it while this one waited for the lock.
                    return BuildResult(name, os.path.join(self.directory, done['file']), False)
                return self._render(name, log)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def _run(self, argv, log_path, timeout=RENDER_TIMEOUT_SECONDS):
        # stderr to a file, never a pipe: an undrained pipe fills and stalls the child.
        with open(log_path, 'ab') as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err)
            try:
                return proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise ContentError(f'{argv[0]} ran past {timeout} s') from None
            except BaseException:
                proc.kill()
                proc.wait()
                raise

    def _probe_packets(self, path, stream) -> list[tuple[int, int]]:
        """(pts, duration) of every packet of the first `stream` ('v' or 'a') stream."""
        out = subprocess.run(
            [self.ffprobe, '-v', 'error', '-select_streams', f'{stream}:0', '-show_entries',
             'packet=pts,duration', '-of', 'csv=p=0', path],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=600)
        if out.returncode:
            raise ContentError(f'ffprobe could not read {path}: {out.stderr.strip()[-300:]}')
        rows = []
        for line in out.stdout.splitlines():
            parts = line.strip().split(',')
            if len(parts) >= 2 and parts[0] not in ('', 'N/A'):
                rows.append((int(parts[0]), int(parts[1]) if parts[1] not in ('', 'N/A') else 0))
        return rows

    def _render(self, name, log) -> BuildResult:
        p = PROFILES[name]
        started = _dt.datetime.now(_dt.timezone.utc)
        t0 = time.monotonic()
        if log:
            log(f'rendering {name} ({p.summary}, {float(p.seconds):.3f} s loop)')
        work = tempfile.mkdtemp(prefix=f'.render-{name}-', dir=self.directory)
        log_path = os.path.join(work, 'render.log')
        try:
            ext = 'mp3' if p.container == 'mp3' else 'ts'
            raw = os.path.join(work, f'raw.{ext}')
            rc = self._run([self.ffmpeg, *render_argv(p, raw, self._font_tag(p))], log_path)
            if rc:
                raise ContentError(f'{name}: ffmpeg exited {rc}: {_tail(log_path)}')
            final = os.path.join(work, f'loop.{ext}')
            facts = (self._finish_mp3(p, raw, final) if p.container == 'mp3'
                     else self._finish_ts(p, raw, final, log_path))
            key, build = self.spec_key(name), self._build_key()
            stem = f'{name}-{key}-{build}'
            sha = _sha256(final)
            size = os.path.getsize(final)
            manifest = {
                'profile': name, 'spec': key, 'build': build, 'file': f'{stem}.{ext}',
                'container': p.container, 'sha256': sha, 'bytes': size,
                'length90': p.length90, 'seconds': float(p.seconds),
                'ffmpeg': self.ffmpeg_version(),
                'font': self._font_digest if self._font_tag(p) else None,
                'rendered_at': started.isoformat(timespec='seconds'),
                'render_seconds': round(time.monotonic() - t0, 1), **facts,
            }
            os.replace(final, os.path.join(self.directory, f'{stem}.{ext}'))
            tmp_json = os.path.join(work, 'manifest.json')
            with open(tmp_json, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2, sort_keys=True)
            os.replace(tmp_json, os.path.join(self.directory, f'{stem}.json'))
            if log:
                log(f'rendered {name}: {size / 1e6:.1f} MB in {manifest["render_seconds"]} s')
            return BuildResult(name, os.path.join(self.directory, f'{stem}.{ext}'), True,
                               manifest['render_seconds'], size)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _finish_ts(self, p, raw, final, log_path) -> dict:
        video = self._probe_packets(raw, 'v')
        audio = self._probe_packets(raw, 'a')
        if len(video) != p.frames:
            raise ContentError(f'{p.name}: rendered {len(video)} video frames, not {p.frames}')
        v0 = min(pts for pts, _ in video)
        frame90 = AUDIO_FRAME[p.audio] * PTS_HZ // p.sample_rate
        want = p.samples // AUDIO_FRAME[p.audio]
        # The encoder's priming frame starts audio a frame ahead of video, and its padding can
        # add one at the end: keep exactly the loop's worth, from the first frame within half a
        # frame of the video's start, so audio spans the loop and the seam is seamless.
        first = next((i for i, (pts, _) in enumerate(audio) if pts >= v0 - frame90 // 2), None)
        if first is None or first + want > len(audio):
            raise ContentError(f'{p.name}: {len(audio)} audio frames rendered, cannot keep '
                               f'{want} from the video start')
        rc = self._run([self.ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-y', '-copyts',
                        '-i', raw, '-map', '0', '-c', 'copy', '-bsf:a',
                        f'noise=drop=lt(n\\,{first})+gte(n\\,{first + want})', '-f', 'mpegts',
                        final], log_path)
        if rc:
            raise ContentError(f'{p.name}: the remux exited {rc}: {_tail(log_path)}')
        kept = self._probe_packets(final, 'a')
        v0 = min(pts for pts, _ in self._probe_packets(final, 'v'))
        if len(kept) != want:
            raise ContentError(f'{p.name}: kept {len(kept)} audio frames, not {want}')
        span = kept[-1][0] + frame90 - kept[0][0]
        if span != p.length90:
            raise ContentError(f'{p.name}: audio spans {span} ticks, the loop {p.length90}')
        with open(final, 'rb') as f:
            loop = Loop(f.read(), p.length90, p.name)
        if len(loop.cuts) != p.frames // p.gop:
            raise ContentError(f'{p.name}: {len(loop.cuts)} keyframes, not {p.frames // p.gop}')
        return {'video_frames': len(video), 'audio_frames': want,
                'audio_offset90': kept[0][0] - v0, 'packets': loop.n,
                'keyframes': len(loop.cuts)}

    def _finish_mp3(self, p, raw, final) -> dict:
        with open(raw, 'rb') as f:
            loop = Mp3Loop.parse(f.read(), p.name)
        with open(final, 'wb') as f:
            f.write(loop.data)
        return {'audio_frames': loop.frames, 'sample_rate': loop.sample_rate}


def _profile(name: str) -> Profile:
    p = PROFILES.get(name)
    if p is None:
        raise ContentError(f'no content profile {name!r}; profiles are {", ".join(PROFILES)}')
    return p


def _estimate(p: Profile) -> int:
    """Bytes a render will take on disk: its bitrate over its length, plus MPEG-TS overhead."""
    return int(p.kbps * 1000 / 8 * float(p.seconds) * (1.0 if p.container == 'mp3' else 1.1))


__all__ = ['ContentError', 'Library', 'PROFILES', 'Profile', 'RADIO_PROFILE', 'TS_PROFILES',
           'check_profiles', 'default_dir', 'profile_for_format']
