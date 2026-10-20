"""Lineups: the channels a backend holds, and what each panel and guide site shows of them.

Each backend's lineup is drawn in one pass from a generator of its own, so two backends never
shift each other; a backend's category count follows its channel count, so changing the count
redraws the whole lineup. Each panel then draws its own view of its backend from a generator
of its own: which channels it carries, which it renames or moves to another category, and how
it spells each stream URL.
"""
from .guide import movie_title
from .names import (GENRES, NAME_TAILS, SPORT_WORDS, Decorator, event_occasion,
                    invented_stem, pick_country)
from .rand import Weighted, derive, rng

KIND_LIVE = 'live'
KIND_EVENT = 'event'
KIND_SEPARATOR = 'separator'
KIND_RADIO = 'radio'
KIND_NO_ID = 'no_id'
# Kinds whose stream URL carries the <user>/<pass>/<numeric id> triplet; the other two live on
# a third-party host under a path with no id in it, and must never be rewritten.
TRIPLET_KINDS = (KIND_LIVE, KIND_EVENT, KIND_SEPARATOR)

# How many qualities a channel offered in more than one comes in.
_VARIANT_SIZES = ((2, 55), (3, 20), (4, 10), (5, 7), (6, 5), (8, 3))
_VARIANT_MEAN = sum(s * w for s, w in _VARIANT_SIZES) / sum(w for _, w in _VARIANT_SIZES)
_CATEGORY_FLAVORS = ('PLUS', 'LOCAL', 'EXTRA', 'PREMIUM', 'SELECT', 'BACKUP')
_MEDIAN_TO_MEAN = 0.7


def channel_label(backend: str, ch) -> str:
    """How the ledger and a stream fault name one channel: backend:stream id, or
    backend:radio/<slug> and backend:hls/<slug> for the two kinds with no id in their URL."""
    if ch.kind == KIND_RADIO:
        return f'{backend}:radio/{ch.slug}'
    if ch.kind == KIND_NO_ID:
        return f'{backend}:hls/{ch.slug}'
    return f'{backend}:{ch.stream_id}'


def frac(seed, *parts) -> float:
    """A fixed number in [0, 1) for one yes-or-no question about one thing."""
    return derive(seed, *parts) / 2.0 ** 64


class Bucket:
    """A category at the backend: one country and one genre (or one sport, for event slots)."""
    __slots__ = ('index', 'kind', 'country', 'genre', 'name')

    def __init__(self, index, kind, country, genre, name):
        self.index, self.kind, self.country, self.genre, self.name = (
            index, kind, country, genre, name)


class Station:
    """One schedule of programs. Every quality variant of a channel shares its station."""
    __slots__ = ('key', 'core', 'country', 'genre', 'formed', 'style', 'has_guide')

    def __init__(self, key, core, country, genre, formed, style, has_guide):
        self.key, self.core, self.country, self.genre = key, core, country, genre
        self.formed, self.style, self.has_guide = formed, style, has_guide


class BackendChannel:
    __slots__ = ('index', 'stream_id', 'kind', 'name', 'mark', 'bucket', 'station', 'logo',
                 'format', 'host', 'slug')

    def __init__(self, index, stream_id, kind, name, mark, bucket, station, logo, fmt, host,
                 slug):
        self.index, self.stream_id, self.kind, self.name, self.mark = (
            index, stream_id, kind, name, mark)
        self.bucket, self.station, self.logo, self.format, self.host, self.slug = (
            bucket, station, logo, fmt, host, slug)


class BackendLineup:
    def __init__(self, backend, channels, buckets, stations):
        self.backend = backend
        self.channels = channels
        self.buckets = buckets
        self.stations = stations
        self.needs_thirdparty = any(c.kind in (KIND_RADIO, KIND_NO_ID) for c in channels)


class PanelRow:
    """One channel as a panel lists it."""
    __slots__ = ('channel', 'num', 'name', 'category_id', 'category_name', 'tvg_id', 'shape')

    def __init__(self, channel, num, name, category_id, category_name, tvg_id, shape):
        self.channel, self.num, self.name = channel, num, name
        self.category_id, self.category_name = category_id, category_name
        self.tvg_id, self.shape = tvg_id, shape


class VodRow:
    __slots__ = ('kind', 'vod_id', 'name', 'category', 'ext')

    def __init__(self, kind, vod_id, name, category, ext):
        self.kind, self.vod_id, self.name, self.category, self.ext = (
            kind, vod_id, name, category, ext)


class GuideChannel:
    """A channel element in a guide: its id, the name it is listed under, and whether the
    guide carries programs for it (a real guide's directory often lists more than it fills)."""
    __slots__ = ('guide_id', 'display_name', 'station', 'has_programs', 'icon')

    def __init__(self, guide_id, display_name, station, has_programs, icon):
        self.guide_id, self.display_name, self.station = guide_id, display_name, station
        self.has_programs, self.icon = has_programs, icon


class PanelLineup:
    def __init__(self, panel, rows, categories, guide, vod):
        self.panel = panel
        self.rows = rows
        self.categories = categories    # [(category_id, name)] in the panel's order
        self.guide = guide              # [GuideChannel] in first-listed order
        self.vod = vod


class SiteLineup:
    def __init__(self, site, guide):
        self.site = site
        self.guide = guide


def guide_id(spelling: str, seed, station: Station, owner: str) -> str:
    """A station's id in the spelling one guide (`owner`, a panel or guide site) uses. A
    callsign-style id is the same in every guide that spells ids that way; a numbered one is
    the owner's own. Two guides that name a station differently are what name matching is
    for."""
    letters = ''.join(ch for ch in station.core if ch.isalpha())
    if spelling == 'callsign_dot_country':
        return f'{letters}.{station.country.lower()}'
    if spelling == 'word_dash_number':
        return f'{letters}-{1000 + derive(seed, "guide-id", owner, station.key) % 9000}'
    if spelling == 'numeric':
        return str(10000 + derive(seed, 'guide-id-numeric', owner, station.key) % 90000)
    raise ValueError(f'unknown id spelling {spelling!r}')


def _formats(cal) -> Weighted:
    """Every format a channel may carry, '1080p59.94/h264/aac/2', weighted by the product of
    the profile's per-dimension shares. The profile measures each dimension on its own, so
    treating them as independent is an assumption, and the simplest one the data allows."""
    f = cal.formats
    out = []
    for res, wr in f['resolution'].items():
        for fps, wf in f['frame_rate'].items():
            for vc, wv in f['video_codec'].items():
                for ac, wa in f['audio_codec'].items():
                    for ch, wc in f['audio_channels'].items():
                        out.append((f'{res}{fps}/{vc}/{ac}/{ch}', wr * wf * wv * wa * wc))
    return Weighted(out)


def _buckets(r, count, kind, cat_dec, used_names):
    out = []
    for _ in range(count):
        country = pick_country(r)
        genre = r.choice(SPORT_WORDS) if kind == KIND_EVENT else r.choice(GENRES)
        core = f'{genre} EVENTS' if kind == KIND_EVENT else genre
        base = name = cat_dec.decorate(r, cat_dec.form(r, core), country)
        n = 0
        while name in used_names:
            n += 1
            flavor = _CATEGORY_FLAVORS[(n - 1) % len(_CATEGORY_FLAVORS)]
            name = f'{base} {flavor}' if n <= len(_CATEGORY_FLAVORS) else f'{base} {flavor} {n}'
        used_names.add(name)
        out.append(Bucket(None, kind, country, genre, name))
    return out


def _bucket_weights(r, buckets):
    """A long-tailed spread of channels over categories, as real lineups have: a few hold
    hundreds, most hold a few dozen."""
    ranks = list(range(len(buckets)))
    r.shuffle(ranks)
    return Weighted((b, 1.0 / (rank + 1) ** 0.6) for b, rank in zip(buckets, ranks))


def build_backend(world, backend) -> BackendLineup:
    seed, cal = world.seed, world.calibration
    r = rng(seed, 'backend', backend.name)
    rates = cal.channel_rates
    n = backend.channels

    other = {KIND_EVENT: rates['event_slot'], KIND_SEPARATOR: rates['separator_row'],
             KIND_RADIO: backend.radio_share, KIND_NO_ID: backend.no_id_share}
    live_share = max(0.05, 1.0 - sum(other.values()))
    # The profile's shares are over every channel in a lineup, but only regular channels take
    # a guide id and some marks, so the rates those draw at are set to land the measured share
    # on the lineup as a whole.
    stem_share = min(0.9, cal.same_name_stem_share)
    multi = stem_share / (_VARIANT_MEAN * (1 - stem_share) + stem_share)
    variants = Weighted([(1, 1.0 - multi)] + [(s, multi * w / 100.0) for s, w in _VARIANT_SIZES])
    mean_draw = 1.0 - multi + multi * _VARIANT_MEAN
    kinds = Weighted([(KIND_LIVE, live_share / mean_draw)] + list(other.items()))
    guide_share = min(1.0, cal.tvg_id_coverage / live_share)
    marked = rates['quality_mark'] + rates['superscript_mark']
    always_marked = live_share * stem_share * (_VARIANT_MEAN - 1) / _VARIANT_MEAN
    never_marked = other[KIND_SEPARATOR] + other[KIND_RADIO] + other[KIND_NO_ID]
    drawn = max(0.0, (marked - always_marked) / max(0.05, 1 - always_marked - never_marked))
    dec = Decorator(cal.channel_prefix_style, rates,
                    mark_scale=drawn / marked if marked else 1.0)
    cat_dec = Decorator(cal.category_prefix_style, cal.category_rates)

    used = set()
    # Categories are long-tailed (_bucket_weights), which puts the median category well
    # under the mean; this is the measured ratio of the two for that spread.
    per_category = max(1.0, float(cal.channels_per_category)) / _MEDIAN_TO_MEAN
    regular = _buckets(r, max(1, round(n * (live_share + other[KIND_SEPARATOR]) / per_category)),
                       KIND_LIVE, cat_dec, used)
    events = []
    if other[KIND_EVENT] * n >= 1:
        events = _buckets(r, max(1, round(n * other[KIND_EVENT] / per_category)), KIND_EVENT,
                          cat_dec, used)
    buckets = regular + events
    for i, b in enumerate(buckets):
        b.index = i
    pick_regular = _bucket_weights(r, regular)
    pick_event = _bucket_weights(r, events) if events else None
    fw = _formats(cal)
    with_time = min(1.0, rates['date_or_time'] / rates['event_slot']) if rates['event_slot'] else 0
    slots = {}
    hosts = backend.stream_hosts

    channels, stations = [], []
    sid = 1000 + derive(seed, 'stream-ids', backend.name) % 2_000_000

    def next_id():
        nonlocal sid
        sid += 1 + (r.randrange(1, 50) if r.random() < 0.08 else 0)
        return sid

    def logo(*parts):
        if r.random() >= cal.logo_coverage:
            return None
        return f'{derive(seed, "logo", backend.name, *parts):016x}'

    while len(channels) < n:
        kind = kinds.pick(r)
        if kind == KIND_EVENT and pick_event is None:
            kind = KIND_LIVE
        if kind == KIND_LIVE:
            bucket = pick_regular.pick(r)
            core = f'{invented_stem(r)} {bucket.genre}'
            if r.random() < 0.3:
                core += f' {r.choice(NAME_TAILS)}'
            if r.random() < 0.15:
                core += f' {r.randrange(2, 10)}'
            st = Station(f'{backend.name}:{len(stations)}', core, bucket.country, bucket.genre,
                         dec.form(r, core), dec.style(r), r.random() < guide_share)
            stations.append(st)
            st_logo = logo(st.key)
            marks = [None]
            for _ in range(variants.pick(r) - 1):
                # Every further quality of a channel carries a mark the earlier ones do not.
                for _try in range(12):
                    m = dec.forced_mark(r)
                    if m not in marks:
                        break
                marks.append(m)
            for mark in marks:
                if len(channels) >= n:
                    break
                name = dec.decorate(r, st.formed, st.country, mark=mark, style=st.style)
                channels.append(BackendChannel(
                    len(channels), next_id(), KIND_LIVE, name, mark or '', bucket, st, st_logo,
                    fw.pick(r), r.randrange(hosts), None))
        elif kind == KIND_EVENT:
            bucket = pick_event.pick(r)
            slots[bucket.index] = slots.get(bucket.index, 0) + 1
            timed = r.random() < with_time
            occasion = event_occasion(r) if timed and r.random() < 0.4 else None
            name = dec.event(r, bucket.country, bucket.genre, slots[bucket.index], timed,
                             (invented_stem(r), invented_stem(r)), occasion)
            channels.append(BackendChannel(len(channels), next_id(), KIND_EVENT, name, '',
                                           bucket, None, logo('event', len(channels)),
                                           fw.pick(r), r.randrange(hosts), None))
        elif kind == KIND_SEPARATOR:
            bucket = pick_regular.pick(r)
            channels.append(BackendChannel(len(channels), next_id(), KIND_SEPARATOR,
                                           dec.separator(r, bucket.country, bucket.genre), '',
                                           bucket, None, logo('separator', len(channels)),
                                           fw.pick(r), r.randrange(hosts), None))
        else:
            bucket = pick_regular.pick(r)
            stem = invented_stem(r)
            if kind == KIND_RADIO:
                core = f'{stem} {r.choice(("FM", "Radio", "Hits"))}'
                fmt = 'audio/mp3/2'
            else:
                core = f'{stem} {bucket.genre}'
                fmt = fw.pick(r)
            slug = f'{stem.lower()}-{len(channels)}'
            name = dec.decorate(r, dec.form(r, core), bucket.country, mark='')
            channels.append(BackendChannel(len(channels), next_id(), kind, name, '', bucket,
                                           None, logo('thirdparty', len(channels)), fmt, None,
                                           slug))
    return BackendLineup(backend, channels, buckets, stations)


def build_panel(world, panel) -> PanelLineup:
    seed, cal = world.seed, world.calibration
    backend = world.lineups[panel.backend]
    r = rng(seed, 'panel', panel.name)
    dec = Decorator(cal.channel_prefix_style, cal.channel_rates)
    shapes = Weighted(panel.url_shape.items())
    by_kind = {}
    for b in backend.buckets:
        by_kind.setdefault(b.kind, []).append(b)

    picked = []
    for ch in backend.channels:
        if r.random() >= panel.share:
            continue
        name = ch.name
        if ch.station is not None and r.random() < panel.rename:
            st = ch.station
            name = dec.decorate(r, dec.form(r, st.core), st.country, mark=ch.mark)
        bucket = ch.bucket
        if r.random() < panel.recategorize and len(by_kind[bucket.kind]) > 1:
            bucket = r.choice([b for b in by_kind[bucket.kind] if b is not bucket])
        shape = shapes.pick(r) if ch.kind in TRIPLET_KINDS else ch.kind
        tvg = ''
        if ch.station is not None and ch.station.has_guide:
            tvg = guide_id(panel.id_spelling, seed, ch.station, panel.name)
        picked.append((bucket, ch, name, shape, tvg))

    order = [b.index for b in backend.buckets]
    r.shuffle(order)
    rank = {idx: i for i, idx in enumerate(order)}
    picked.sort(key=lambda t: (rank[t[0].index], t[1].index))
    cat_base = 1 + derive(seed, 'category-ids', panel.name) % 9000
    rows, categories, seen_cat = [], [], set()
    guide, seen_guide = [], set()
    for num, (bucket, ch, name, shape, tvg) in enumerate(picked, 1):
        cid = str(cat_base + bucket.index)
        if cid not in seen_cat:
            seen_cat.add(cid)
            categories.append((cid, bucket.name))
        rows.append(PanelRow(ch, num, name, cid, bucket.name, tvg, shape))
        if tvg and tvg not in seen_guide:
            seen_guide.add(tvg)
            st = ch.station
            guide.append(GuideChannel(
                tvg, st.formed, st,
                frac(seed, 'guide-covered', panel.name, st.key) < panel.guide_coverage,
                ch.logo))
    return PanelLineup(panel, rows, categories, guide, _vod(seed, panel))


def _vod(seed, panel) -> list:
    r = rng(seed, 'vod', panel.name)
    base = 500_000 + derive(seed, 'vod-ids', panel.name) % 400_000
    out = []
    for i in range(panel.vod_rows):
        kind = 'movie' if r.random() < 0.7 else 'series'
        genre = r.choice(('ACTION', 'DRAMA', 'COMEDY', 'FAMILY', 'THRILLER', 'DOCUMENTARY'))
        title = movie_title(r)
        name = f'{title} ({r.randrange(1990, 2026)})' if kind == 'movie' else f'{title} S01'
        out.append(VodRow(kind, base + i * 7, name, f'VOD| {genre}', r.choice(('mkv', 'mp4'))))
    return out


def build_site(world, site) -> SiteLineup:
    seed = world.seed
    guide, seen = [], set()
    for bname in site.backends:
        for st in world.lineups[bname].stations:
            if frac(seed, 'site-covers', site.name, st.key) >= site.coverage:
                continue
            gid = guide_id(site.id_spelling, seed, st, site.name)
            if gid in seen:
                continue
            seen.add(gid)
            guide.append(GuideChannel(gid, st.core, st, True, None))
    return SiteLineup(site, guide)
