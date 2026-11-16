"""The waiting screen's still layers, drawn with Pillow, and the motion ffmpeg adds over them.

Geometry is dev/mockups/55's look 7 (`55-page.css`, the `.d7-*`, `.badge-disc`, `.sweep` and
`.tk*` rules) in the pixels of the 1920x1080 picture. app/sports_screen.py decides what is drawn
(a Screen); this module only draws it.

The picture is drawn with a bleed: MX/MY pixels of extra picture on every side, so the drift
(a crop window moving up to 14 px) never shows an edge. The mockup scaled the picture up 2.5% to
hide the edge instead; a bleed keeps every pixel at the mockup's size and spares ffmpeg a per-frame
scale (dev/changelog/1533).
"""
import logging
import math
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger(__name__)

W, H = 1920, 1080
#: The bleed: the drift's reach plus a margin (the largest move is 14 px across, 10 down).
MX, MY = 16, 12
CW, CH = W + 2 * MX, H + 2 * MY
FPS = 30

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(_ROOT, 'static', 'fonts')
LOGO = os.path.join(_ROOT, 'static', 'img', 'logo', 'channelbin-logo.png')
#: Inter 4.1's static faces. TTF, not the web pages' .woff2: those are a Latin subset with no 800,
#: and a font a renderer cannot read falls back to another with no error (dev/changelog/1528).
_FONT_FILES = {400: 'Inter-Regular.ttf', 600: 'Inter-SemiBold.ttf', 700: 'Inter-Bold.ttf',
               800: 'Inter-ExtraBold.ttf'}

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
LIVE = (0xff, 0x8a, 0x80)
SOFT = (0xd7, 0xda, 0xe0)
LATER = (0xae, 0xb4, 0xbe)
EMPTY = (0xc3, 0xc7, 0xcf)
BAND_MID = (0x16, 0x18, 0x1c)
TICKER_BG = (0x13, 0x14, 0x17)
AMBER = (0xd9, 0xa6, 0x2e)

#: Redirect mode's crawl, in pixels a frame: 240 px/s at 30 fps, whole pixels so every frame of
#: the text is sharp.
CRAWL_PX = 8
#: The shimmer crosses every 10 s and the drift goes round every 30 s, each scaled to a whole
#: number of cycles inside the loop.
SHIMMER_FRAMES = 10 * FPS
DRIFT_FRAMES = 30 * FPS
#: Every loop is a whole number of these frames: 16 frames at 30 fps are exactly 25 AAC frames of
#: 1024 samples at 48 kHz, so the clip's audio ends where its video does and a loop's seam leaves
#: no gap in either but the encoder's own priming (dev/changelog/1533).
LOOP_UNIT = 16
#: The loop with no ticker: about 30 s, a whole number of LOOP_UNITs.
PLAIN_LOOP_FRAMES = 57 * LOOP_UNIT
#: The checkered flag of a field event moves 60 px/s; one tile is 60 px.
FLAG_TILE = 60
FLAG_PX = 2
#: The drift's keyframes (x, y) in pixels the picture moves, as `dw-drift` in the mockup.
DRIFT = ((0, 0), (14, -9), (-4, 10), (-14, -4))

TICKER_TOP = 944
TICKER_H = 100
TICKER_BORDER = 4
CRAWL_SIZE = 42
LABEL_SIZE = 38


def hex_rgb(color: str) -> tuple:
    n = int(color[1:], 16)
    return n >> 16, (n >> 8) & 255, n & 255


@lru_cache(maxsize=64)
def font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(FONT_DIR, _FONT_FILES[weight]), size)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def _length(text: str, f, spacing: float = 0) -> float:
    return f.getlength(text) + spacing * len(text)


def _baseline(top: float, f, line_height: float = None) -> float:
    """The baseline of a line whose box starts at `top`, as CSS places it: the font's ascent and
    descent centered in the line box ("normal" is the two together)."""
    asc, desc = f.getmetrics()
    box = line_height if line_height is not None else asc + desc
    return top + (box - (asc + desc)) / 2 + asc


def _wrap(text: str, f, max_w: float) -> list:
    """Greedy word wrap; a word wider than the line is left on a line of its own."""
    lines, line = [], ''
    for word in text.split():
        trial = f'{line} {word}' if line else word
        if line and f.getlength(trial) > max_w:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def ellipsize(text: str, f, max_w: float, spacing: float = 0) -> str:
    if _length(text, f, spacing) <= max_w:
        return text
    while text and _length(text + '...', f, spacing) > max_w:
        text = text[:-1]
    return text.rstrip() + '...'


def fit_lines(text: str, weight: int, size: int, max_w: float, max_lines: int, min_size: int) -> tuple:
    """(font, lines) for `text` in at most `max_lines` lines of `max_w`: wrapped at `size` first,
    then smaller a step at a time down to `min_size`, then ellipsized on the last line."""
    for s in range(size, min_size - 1, -2):
        f = font(weight, s)
        lines = _wrap(text, f, max_w)
        if len(lines) <= max_lines and all(f.getlength(x) <= max_w for x in lines):
            return f, lines
    f = font(weight, min_size)
    lines = _wrap(text, f, max_w)
    kept = lines[:max_lines]
    rest = ' '.join(lines[max_lines - 1:])
    kept[-1] = ellipsize(rest + ('' if len(lines) <= max_lines else ' '), f, max_w)
    return f, [x if f.getlength(x) <= max_w else ellipsize(x, f, max_w) for x in kept]


def fit_line(text: str, weight: int, size: int, max_w: float, min_size: int, spacing_em: float = 0) -> tuple:
    """(font, text) on one line: smaller a step at a time down to `min_size`, then ellipsized."""
    for s in range(size, min_size - 1, -2):
        f = font(weight, s)
        if _length(text, f, spacing_em * s) <= max_w:
            return f, text
    f = font(weight, min_size)
    return f, ellipsize(text, f, max_w, spacing_em * min_size)


class _Canvas:
    """One 1920x1080 picture being drawn, with the shadowed text gathered on a mask so one blur
    serves every shadow."""

    def __init__(self):
        self.img = Image.new('RGB', (W, H), BLACK)
        self.draw = ImageDraw.Draw(self.img, 'RGBA')
        self._shadowed = []

    def text(self, xy, text, f, fill, *, anchor='ls', spacing=0.0, shadow=False, alpha=255):
        """Draw a line of text; with `shadow` it waits for shadow_pass(), which lays the shadow
        first and the text over it."""
        if shadow:
            self._shadowed.append((xy, text, f, anchor, spacing, fill, alpha))
            return
        color = fill + (alpha,)
        if not spacing:
            self.draw.text(xy, text, font=f, fill=color, anchor=anchor)
            return
        # Letter-spaced text (CSS letter-spacing): one glyph at a time from the left edge.
        x, y = xy
        width = _length(text, f, spacing)
        if anchor[0] == 'm':
            x -= width / 2
        elif anchor[0] == 'r':
            x -= width
        for ch in text:
            self.draw.text((x, y), ch, font=f, fill=color, anchor='l' + anchor[1])
            x += f.getlength(ch) + spacing

    def shadow_pass(self):
        """Draw the waiting shadowed text: its shadow (0 4px 18px rgba(0,0,0,.6)) over what is
        drawn so far, then the text."""
        if not self._shadowed:
            return
        mask = Image.new('L', (W, H), 0)
        d = ImageDraw.Draw(mask)
        for (x, y), text, f, anchor, spacing, _fill, _alpha in self._shadowed:
            if spacing:
                width = _length(text, f, spacing)
                x0 = x - width / 2 if anchor[0] == 'm' else x - width if anchor[0] == 'r' else x
                for ch in text:
                    d.text((x0, y + 4), ch, font=f, fill=255, anchor='l' + anchor[1])
                    x0 += f.getlength(ch) + spacing
            else:
                d.text((x, y + 4), text, font=f, fill=255, anchor=anchor)
        mask = mask.filter(ImageFilter.GaussianBlur(9)).point(lambda v: v * 6 // 10)
        self.img.paste(BLACK, (0, 0), mask)
        waiting, self._shadowed = self._shadowed, []
        for xy, text, f, anchor, spacing, fill, alpha in waiting:
            self.text(xy, text, f, fill, anchor=anchor, spacing=spacing, alpha=alpha)


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def _color_at(stops, t: float) -> tuple:
    if t <= stops[0][0]:
        return stops[0][1]
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if t <= p1:
            f = 0 if p1 == p0 else (t - p0) / (p1 - p0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
    return stops[-1][1]


def gradient(size, angle: float, stops, mode: str = 'RGB') -> Image.Image:
    """A CSS linear-gradient(<angle>deg, ...) filling `size`. `stops` are [(position, color)], a
    position a fraction of the gradient line (1.3 is 130%), a color a tuple in `mode`."""
    w, h = size
    a = math.radians(angle)
    dx, dy = math.sin(a), -math.cos(a)
    length = abs(w * dx) + abs(h * dy)
    k = 2048
    strip = Image.new(mode, (k, 3))
    if mode == 'L':
        stops = [(p, (v,)) for p, v in stops]
    row = [_color_at(stops, i / (k - 1)) for i in range(k)]
    if mode == 'L':
        row = [v[0] for v in row]
    strip.putdata(row * 3)
    s = (k - 1) / length
    c = (-(w / 2) * dx - (h / 2) * dy) * s + (k - 1) / 2
    return strip.transform(size, Image.AFFINE, (dx * s, dy * s, c, 0, 0, 1.5), resample=Image.BILINEAR)


def _aa_mask(size, paint, scale: int = 4) -> Image.Image:
    """An anti-aliased mask: `paint(draw, scale)` draws at `scale` times and it is shrunk."""
    big = Image.new('L', (size[0] * scale, size[1] * scale), 0)
    paint(ImageDraw.Draw(big), scale)
    return big.resize(size, Image.LANCZOS)


def _polygon_mask(size, points) -> Image.Image:
    return _aa_mask(size, lambda d, s: d.polygon([(x * s, y * s) for x, y in points], fill=255))


def _disc_mask(d: int) -> Image.Image:
    return _aa_mask((d, d), lambda dr, s: dr.ellipse((0, 0, d * s - 1, d * s - 1), fill=255))


@lru_cache(maxsize=8)
def _logo(size: int) -> Image.Image:
    return Image.open(LOGO).convert('RGBA').resize((size, size), Image.LANCZOS)


#: The largest badge that is decoded. The download is checked by its header and its bytes only
#: (app/sports_images.py), and a PNG of a few hundred KB can declare a picture that decodes to
#: hundreds of MB; TheSportsDB's badges are 512 px square.
BADGE_MAX_PIXELS = 4096 * 4096


@lru_cache(maxsize=32)
def _badge_image(path: str, box: int, _stamp) -> Image.Image:
    """A downloaded badge fitted into a `box` square. `_stamp` (size, mtime) keys the cache to the
    file as it is now."""
    with Image.open(path) as im:
        if im.width * im.height > BADGE_MAX_PIXELS:
            raise ValueError(f'it is {im.width} x {im.height}, larger than a badge is ever drawn from')
        im = im.convert('RGBA')
        im.thumbnail((box, box), Image.LANCZOS)
        return im.copy()


def _badge_for(path):
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return path, (st.st_size, int(st.st_mtime))


def _decoded_badge(badge, box: int):
    """The badge fitted into `box`, or None when there is none or its file cannot be decoded: the
    disc then shows the team's code, as it does with no badge. A badge is a download, and one bad
    file must not cost a channel its whole waiting screen (dev/docs/BUGS.md 2026-11-15 01:36 PM)."""
    if badge is None:
        return None
    try:
        return _badge_image(badge[0], box, badge[1])
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        log.warning('The badge %s cannot be read as a picture (%s), so the waiting screen draws the '
                    "team's code in its place", badge[0], exc)
        return None


def badge_disc(cv: _Canvas, cx: float, cy: float, side, *, inner: int, border: int, ring: int,
               code_size: int, drop: bool):
    """`.badge-disc`: the team's first color with its second as the border and a white ring, its
    short code in the middle, or its downloaded badge on white in place of the code."""
    first, second = (hex_rgb(c) for c in side.colors)
    outer = inner + 2 * border + 2 * ring
    left, top = round(cx - outer / 2), round(cy - outer / 2)
    if drop:
        # box-shadow: 0 16px 40px rgba(0,0,0,.5)
        pad = 60
        sh = Image.new('L', (outer + 2 * pad, outer + 2 * pad), 0)
        sh.paste(_disc_mask(outer - 2 * ring), (pad + ring, pad + ring))
        sh = sh.filter(ImageFilter.GaussianBlur(20)).point(lambda v: v // 2)
        cv.img.paste(BLACK, (left - pad, top - pad + 16), sh)
    cv.img.paste(WHITE, (left, top), _disc_mask(outer))
    cv.img.paste(second, (left + ring, top + ring), _disc_mask(outer - 2 * ring))
    im = _decoded_badge(_badge_for(side.badge), round(inner * 0.8))
    fill = WHITE if im is not None else first
    cv.img.paste(fill, (left + ring + border, top + ring + border), _disc_mask(inner))
    if im is not None:
        cv.img.paste(im, (round(cx - im.width / 2), round(cy - im.height / 2)), im)
        return
    f, code = fit_line(side.code, 800, code_size, inner * 0.9, max(10, code_size // 2))
    # text-shadow: 0 3px 8px rgba(0,0,0,.6), local to the disc.
    pad = 20
    tw = round(inner + 2 * pad)
    sh = Image.new('L', (tw, tw), 0)
    ImageDraw.Draw(sh).text((tw / 2, tw / 2 + 3), code, font=f, fill=255, anchor='mm')
    sh = sh.filter(ImageFilter.GaussianBlur(4)).point(lambda v: v * 6 // 10)
    cv.img.paste(BLACK, (round(cx - tw / 2), round(cy - tw / 2)), sh)
    cv.draw.text((cx, cy), code, font=f, fill=WHITE, anchor='mm')


def _checker(size, cell: int, phase: int = 0, alpha: int = 255) -> Image.Image:
    """`repeating-conic-gradient(#fff 0 25%, #000 0 50%)` on a 2*cell tile: white top right and
    bottom left. `phase` shifts it left, for the moving flag."""
    w, h = size
    tile = Image.new('RGBA', (2 * cell, 2 * cell), BLACK + (alpha,))
    d = ImageDraw.Draw(tile)
    d.rectangle((cell, 0, 2 * cell - 1, cell - 1), fill=WHITE + (alpha,))
    d.rectangle((0, cell, cell - 1, 2 * cell - 1), fill=WHITE + (alpha,))
    out = Image.new('RGBA', size)
    for y in range(0, h, 2 * cell):
        for x in range(-phase, w, 2 * cell):
            out.paste(tile, (x, y))
    return out


# ---------------------------------------------------------------------------
# Look 7
# ---------------------------------------------------------------------------

HERO_H = 620


def _hero(cv: _Canvas, screen, game):
    if game is not None and not game.field:
        away, home = (hex_rgb(game.away.colors[0]), hex_rgb(game.home.colors[0]))
        a = gradient((W, HERO_H), 160, [(0, away), (1.3, BLACK)])
        h = gradient((W, HERO_H), 200, [(0, home), (1.3, BLACK)])
        mask = _polygon_mask((W, HERO_H), [(0, 0), (W * .56, 0), (W * .44, HERO_H), (0, HERO_H)])
        h.paste(a, (0, 0), mask)
        cv.img.paste(h, (0, 0))
    else:
        cv.img.paste(gradient((W, HERO_H), 160, [(0, hex_rgb(screen.color)), (1.2, BLACK)]), (0, 0))
    # .d7-hero::after: the last 160 px fade to black.
    fade = gradient((W, 160), 180, [(0, 0), (1, 255)], mode='L')
    cv.img.paste(BLACK, (0, HERO_H - 160), fade)


def _bug(cv: _Canvas, channel: str):
    """`.d7-bug`: the logo and the channel's name, on their own line at the top right."""
    f, name = fit_line(channel, 700, 30, 1100, 22)
    right = W - 110
    width = f.getlength(name)
    logo = _logo(70)
    x = right - width - 14 - 70
    alpha = Image.new('L', logo.size, 0)
    alpha.paste(logo.getchannel('A').point(lambda v: v * 9 // 10))
    cv.img.paste(logo.convert('RGB'), (round(x), 46), alpha)
    cv.text((right, _baseline(46 + (70 - sum(f.getmetrics())) / 2, f)), name, f, WHITE, anchor='rs', alpha=230)


def _kicker(cv: _Canvas, text: str, top: int = 136) -> int:
    """`.d2-kicker` in look 7: an uppercase pill. Returns the bottom of the pill."""
    text = text.upper()
    f, text = fit_line(text, 700, 38, 1600, 24, spacing_em=.08)
    spacing = .08 * f.size
    width = _length(text, f, spacing) + 68
    height = sum(f.getmetrics()) + 24
    left = (W - width) / 2
    pill = _aa_mask((round(width), round(height)),
                    lambda d, s: d.rounded_rectangle((0, 0, width * s, height * s), radius=height * s / 2, fill=255))
    cv.img.paste(BLACK, (round(left), top), pill.point(lambda v: v * 55 // 100))
    cv.text((left + 34, _baseline(top + 12, f)), text, f, WHITE, spacing=spacing)
    return top + round(height)


def _team_lines(name: str):
    return fit_lines(name, 800, 54, 562, 2, 34)


def _matchup(cv: _Canvas, game, row_top: int) -> int:
    """`.d7-row`: away's name and badge, "at", home's badge and name. Returns the row's bottom."""
    at = font(700, 46)
    at_w = at.getlength('at')
    total = 760 * 2 + 40 * 2 + at_w
    x0 = (W - total) / 2
    disc = 150 + 2 * 9
    row_h = disc
    cy = row_top + row_h / 2
    away_right = x0 + 760
    home_left = away_right + 40 + at_w + 40
    for side, disc_cx, text_x, anchor in (
            (game.away, away_right - disc / 2, away_right - disc - 30, 'rs'),
            (game.home, home_left + disc / 2, home_left + disc + 30, 'ls')):
        badge_disc(cv, disc_cx, cy, side, inner=150, border=9, ring=6, code_size=50, drop=True)
        f, lines = _team_lines(side.name)
        lh = 1.08 * f.size
        top = cy - lh * len(lines) / 2
        for i, line in enumerate(lines):
            cv.text((text_x, _baseline(top + i * lh, f, lh)), line, f, WHITE, anchor=anchor, shadow=True)
    cv.text((away_right + 40, _baseline(cy - sum(at.getmetrics()) / 2, at)), 'at', at, WHITE, alpha=217)
    return row_top + row_h


def _solo_title(cv: _Canvas, text: str, top: int) -> int:
    f, lines = fit_lines(text, 800, 84, W - 320, 2, 52)
    lh = 1.05 * f.size
    for i, line in enumerate(lines):
        cv.text((W / 2, _baseline(top + i * lh, f, lh)), line, f, WHITE, anchor='ms', shadow=True)
    return round(top + lh * len(lines))


def _band(cv: _Canvas, top: int, game, ends, when: str):
    """One `.d7-band`: a later game as an edge-to-edge stripe in its two teams' colors."""
    left, right = (hex_rgb(c) for c in ends)
    stops = [(0, left), (.17, left), (.1715, BAND_MID), (.8285, BAND_MID), (.83, right), (1, right)]
    cv.img.paste(gradient((W, 72), 100, stops), (0, top))
    cy = top + 36
    for side, cx in ((game.away, 165), (game.home, W - 165)):
        if side is None:
            chk = _checker((60, 60), 15).convert('RGB')
            cv.img.paste(chk, (cx - 30, round(cy - 30)))
        else:
            badge_disc(cv, cx, cy, side, inner=60, border=5, ring=3, code_size=20, drop=False)
    f, when = fit_line(when, 700, 34, 400 - 34 - 16, 26)
    cv.text((330 + 34, _baseline(cy - sum(f.getmetrics()) / 2, f)), when, f, SOFT)
    f, name = fit_line(game.name, 600, 36, 860 - 20, 28)
    cv.text((730, _baseline(cy - sum(f.getmetrics()) / 2, f)), name, f, WHITE)


def _note(cv: _Canvas, text: str):
    """`.d7-note`: one line at the bottom, two if it must."""
    f, lines = fit_lines(text, 400, 32, W - 220, 2, 26)
    box = sum(f.getmetrics())
    top = H - 70 - box * len(lines)
    for i, line in enumerate(lines):
        cv.text((W / 2, _baseline(top + i * box, f)), line, f, WHITE, anchor='ms', alpha=230)


def _empty(cv: _Canvas, text: str):
    """`.d7-empty`: the explanation when there is no game to show."""
    f, lines = fit_lines(text, 400, 38, W - 400, 4, 30)
    lh = 1.35 * f.size
    for i, line in enumerate(lines):
        cv.text((W / 2, _baseline(640 + i * lh, f, lh)), line, f, EMPTY, anchor='ms')


def _ticker_bar(cv: _Canvas, label: str) -> int:
    """`.tk` without its crawl: the bar and the amber label. Returns the label's right edge,
    where the crawl begins."""
    d = cv.draw
    d.rectangle((0, TICKER_TOP, W, H), fill=TICKER_BG)
    d.rectangle((0, TICKER_TOP, W, TICKER_TOP + TICKER_BORDER - 1), fill=AMBER)
    f, text = fit_line(label.upper(), 800, LABEL_SIZE, 900, 28, spacing_em=.04)
    spacing = .04 * f.size
    right = round(110 + _length(text, f, spacing) + 44)
    top = TICKER_TOP + TICKER_BORDER
    d.rectangle((0, top, right - 1, TICKER_TOP + TICKER_H - 1), fill=AMBER)
    cv.text((110, _baseline(top + (TICKER_H - TICKER_BORDER - sum(f.getmetrics())) / 2, f)),
            text, f, TICKER_BG, spacing=spacing)
    return right


def still(screen) -> tuple:
    """(picture, crawl_left): the 1920x1080 still of `screen`, everything that does not move, and
    where the ticker's crawl starts (None with no ticker)."""
    cv = _Canvas()
    words = screen.words
    game = screen.game
    _hero(cv, screen, game)
    _bug(cv, screen.channel)
    bottom = _kicker(cv, words.kicker)
    if game is None:
        _solo_title(cv, words.headline, bottom + 50)
        cv.shadow_pass()
        _empty(cv, words.note)
    else:
        if game.field:
            bottom = _solo_title(cv, game.name, bottom + 50)
        else:
            bottom = _matchup(cv, game, bottom + 30)
        cv.shadow_pass()
        f = font(800, 62)
        top = bottom + 22
        cv.text((W / 2, _baseline(top, f)), words.when, f, LIVE if words.live else WHITE, anchor='ms', shadow=True)
        top += sum(f.getmetrics())
        if game.venue:
            fv, venue = fit_line(game.venue, 400, 32, W - 320, 26)
            cv.text((W / 2, _baseline(top + 4, fv)), venue, fv, WHITE, anchor='ms', alpha=217)
        cv.shadow_pass()
        if screen.later:
            fl, label = fit_line(f'Later on {screen.channel}'.upper(), 700, 30, W - 220, 22, spacing_em=.1)
            cv.text((110, _baseline(636, fl)), label, fl, LATER, spacing=.1 * fl.size)
            for i, later in enumerate(screen.later[:3]):
                ends = ((later.away.colors[0], later.home.colors[0]) if not later.field
                        else (screen.color, screen.color))
                _band(cv, 688 + i * 82, later, ends, f'{later.day}  {later.clock}'.strip())
        if not words.ticker:
            _note(cv, words.note)
    crawl_left = _ticker_bar(cv, words.ticker_label) if words.ticker else None
    cv.shadow_pass()
    return cv.img, crawl_left


def _bleed(img: Image.Image) -> Image.Image:
    """The picture with MX/MY of its own edges repeated around it, for the drift to reveal."""
    out = Image.new('RGB', (CW, CH))
    out.paste(img, (MX, MY))
    out.paste(img.crop((0, 0, 1, H)).resize((MX, H)), (0, MY))
    out.paste(img.crop((W - 1, 0, W, H)).resize((MX, H)), (MX + W, MY))
    out.paste(out.crop((0, MY, CW, MY + 1)).resize((CW, MY)), (0, 0))
    out.paste(out.crop((0, MY + H - 1, CW, MY + H)).resize((CW, MY)), (0, MY + H))
    return out


def crawl_pass(screen) -> str:
    """One pass of the ticker's text: its sentences, each ended, with a dot between."""
    return ''.join(f'{s}.   ·   ' for s in screen.words.ticker)


# ---------------------------------------------------------------------------
# The layers ffmpeg moves, and the loop
# ---------------------------------------------------------------------------

def loop_frames(screen) -> int:
    """How many frames the clip is before it repeats: one pass of the ticker's text at CRAWL_PX
    a frame (the pass padded to a whole number of LOOP_UNITs), or PLAIN_LOOP_FRAMES with no
    ticker."""
    if not screen.words.ticker:
        return PLAIN_LOOP_FRAMES
    return _crawl_width(screen) // CRAWL_PX


def _crawl_width(screen) -> int:
    width = font(600, CRAWL_SIZE).getlength(crawl_pass(screen))
    step = CRAWL_PX * LOOP_UNIT
    return max(1, math.ceil(width / step)) * step


def _cycles(frames: int, period: int) -> float:
    """A period close to `period` that goes into `frames` a whole number of times."""
    return frames / max(1, round(frames / period))


def _field_hero(screen) -> bool:
    """The one-color hero with the checkered strip: a field event, or no game on a field league."""
    return screen.game.field if screen.game is not None else screen.field


def layers(screen, folder: str) -> dict:
    """Write the clip's layers into `folder` and return what the ffmpeg command needs: the paths,
    the loop length and each motion's numbers."""
    picture, crawl_left = still(screen)
    base = _bleed(picture)
    frames = loop_frames(screen)
    out = {'frames': frames, 'base': os.path.join(folder, 'base.png')}
    if _field_hero(screen):
        # .d2-flag: the checkered strip across the top, moving, at .9 opacity. Drawn into the
        # bleed too, so the drift never uncovers a still copy of it.
        # The tiles start at the picture's top edge, MY into the strip.
        skip = (FLAG_TILE - MY % FLAG_TILE) % FLAG_TILE
        flag = _checker((CW + FLAG_TILE, skip + MY + 60), FLAG_TILE // 2, alpha=230)
        flag = flag.crop((0, skip, CW + FLAG_TILE, skip + MY + 60))
        out['flag'] = os.path.join(folder, 'flag.png')
        flag.save(out['flag'])
        cycles = max(1, round(frames * FLAG_PX / FLAG_TILE))
        out['flag_step'] = FLAG_TILE * cycles / frames
    base.save(out['base'])
    if crawl_left is not None:
        width = _crawl_width(screen)
        visible = CW - (MX + crawl_left)
        f = font(600, CRAWL_SIZE)
        text = crawl_pass(screen)
        strip_h = TICKER_H - TICKER_BORDER
        strip = Image.new('RGB', (width + visible, strip_h), TICKER_BG)
        d = ImageDraw.Draw(strip)
        y = _baseline((strip_h - sum(f.getmetrics())) / 2, f)
        for x in range(0, width + visible, width):
            d.text((x, y), text, font=f, fill=WHITE, anchor='ls')
        out['crawl'] = os.path.join(folder, 'crawl.png')
        strip.save(out['crawl'])
        out.update(crawl_width=width, crawl_visible=visible, crawl_x=MX + crawl_left,
                   crawl_y=MY + TICKER_TOP + TICKER_BORDER)
    # .sweep: a light band at 100deg, 40% to 60% of the way across, white at .11.
    band = gradient((W, H), 100, [(0, 0), (.4, 0), (.5, 28), (.6, 0), (1, 0)], mode='L')
    box = band.getbbox()
    band = band.crop(box)
    sweep = Image.new('RGBA', band.size, WHITE + (0,))
    sweep.putalpha(band)
    out['sweep'] = os.path.join(folder, 'sweep.png')
    sweep.save(out['sweep'])
    out['sweep_left'] = box[0]
    out['shimmer_period'] = _cycles(frames, SHIMMER_FRAMES)
    out['drift_period'] = _cycles(frames, DRIFT_FRAMES)
    return out


def drift_expr(axis: int, period: float) -> str:
    """The crop window's x (axis 0) or y (axis 1) at frame n: the bleed less the picture's move,
    eased between DRIFT's keyframes (smoothstep, close to CSS ease-in-out), whole pixels."""
    rest = MX if axis == 0 else MY
    points = [p[axis] for p in DRIFT] + [DRIFT[0][axis]]
    seg = ''.join(f'if(eq(ld(1),{i}),{a}+({b}-{a})*ld(3),' for i, (a, b) in enumerate(zip(points, points[1:])))
    pick = seg + '0' + ')' * (len(points) - 1)
    return (f'st(0,mod(n,{period:.6f})/{period:.6f}*{len(DRIFT)});st(1,floor(ld(0)));'
            f'st(2,ld(0)-ld(1));st(3,ld(2)*ld(2)*(3-2*ld(2)));{rest}-floor({pick}+0.5)')


def sweep_expr(left: int, period: float) -> str:
    """The shimmer's x at frame n: held off the left edge for 60% of its period, then across the
    whole picture (`d2-sweep` keyframes: translateX(-100%) to 100%)."""
    return (f'st(0,mod(n,{period:.6f})/{period:.6f});'
            f'{left}+if(lt(ld(0),0.6),-{W},-{W}+(ld(0)-0.6)/0.4*{2 * W})')


def _still_input(index: int, fmt: str) -> str:
    """A picture read once and repeated as frame n of a 30 fps stream. An image input looped by
    the demuxer (-loop 1) is decoded again for every frame: 13 s of a 30 s clip's make, measured."""
    return f"[{index}:v]format={fmt},loop=loop=-1:size=1,settb=1/{FPS},setpts=N"


def filtergraph(spec: dict) -> str:
    """The filter_complex for `spec` (layers()). Inputs: 0 the bleed picture, then the crawl, the
    flag and the sweep, each present in that order when the spec has it. Everything stays in
    yuv420p: the same graph in RGB cost 20 s of a 30 s clip's make."""
    parts = [_still_input(0, 'yuv420p') + '[b0]']
    n, cur = 1, 'b0'
    if 'crawl' in spec:
        parts.append(_still_input(n, 'yuv420p')
                     + f",crop=w={spec['crawl_visible']}:h={TICKER_H - TICKER_BORDER}:"
                     f"x='mod(n*{CRAWL_PX},{spec['crawl_width']})':y=0:exact=1[cr]")
        parts.append(f"[{cur}][cr]overlay=x={spec['crawl_x']}:y={spec['crawl_y']}[b1]")
        cur, n = 'b1', n + 1
    if 'flag' in spec:
        parts.append(_still_input(n, 'yuva420p') + '[fl]')
        # Moving right, as `d2-flag`'s background-position does.
        parts.append(f"[{cur}][fl]overlay=x='floor(mod(n*{spec['flag_step']:.6f},{FLAG_TILE}))-{FLAG_TILE}':"
                     f"y=0:eval=frame[b2]")
        cur, n = 'b2', n + 1
    parts.append(_still_input(n, 'yuva420p') + '[sw]')
    parts.append(f"[{cur}]crop=w={W}:h={H}:x='{drift_expr(0, spec['drift_period'])}':"
                 f"y='{drift_expr(1, spec['drift_period'])}':exact=1[d]")
    parts.append(f"[d][sw]overlay=x='{sweep_expr(spec['sweep_left'], spec['shimmer_period'])}':y=0:"
                 f"eval=frame[v]")
    return ';\n'.join(parts)


def still_layers(screen, folder: str) -> list:
    """The quick still's one picture: the whole 1920x1080 frame at rest, the ticker's text and a
    field event's flag drawn where they start. Returns [path]."""
    picture, crawl_left = still(screen)
    if crawl_left is not None:
        f = font(600, CRAWL_SIZE)
        strip_h = TICKER_H - TICKER_BORDER
        top = TICKER_TOP + TICKER_BORDER
        d = ImageDraw.Draw(picture)
        d.rectangle((crawl_left, top, W, H), fill=TICKER_BG)
        crawl = Image.new('RGB', (W - crawl_left, strip_h), TICKER_BG)
        ImageDraw.Draw(crawl).text((0, _baseline((strip_h - sum(f.getmetrics())) / 2, f)), crawl_pass(screen),
                                   font=f, fill=WHITE, anchor='ls')
        picture.paste(crawl, (crawl_left, top))
    if _field_hero(screen):
        flag = _checker((W, 60), FLAG_TILE // 2, alpha=230)
        picture.paste(flag, (0, 0), flag)
    path = os.path.join(folder, 'still.png')
    picture.save(path)
    return [path]


def still_graph() -> str:
    return _still_input(0, 'yuv420p') + '[v]'


def layer_inputs(spec: dict) -> list:
    """The image paths filtergraph() reads, in its input order after the bleed picture."""
    return [spec[k] for k in ('crawl', 'flag', 'sweep') if k in spec]
