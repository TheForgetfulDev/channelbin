"""The EPG similarity scan: which EPG ids carry the same schedule, across the whole guide.

The TV Guide holds one schedule per (source, file id read) - `epg_entries` for the source
that is each channel's guide, `epg_alternate_entries` for the sources that are not
(DESIGN-epg-sources.md §4). Many ids carry one schedule: a station listed under its call
sign on one source and its network name on another, a provider's own id and an external
file's. The compare-sources page answers that for one channel and the group page's What's
On card for one group (dev/changelog/1108, 1324); this reads every schedule the database
holds and says which ones agree, and how good each copy is, so a person can choose a
source and an id from evidence. Rulings: DESIGN-epg-sources.md §12.8, dev/changelog/1325.

How it stays linear at guide scale (19,887 schedules and 572,000 listings on prod's
2026-10-28 snapshot, 36 s for the whole pass):

1. **One streamed read per table, listings from now on**, each row folded into its
   (channel, source) unit as it arrives: an interned title, the lengths and short digests
   of the other fields, the times. No description text is held.
2. **Seven fingerprints per unit** - a hash over its listings in time order of the facet
   named: programs (comparison_title() + start, what compare_listings() would pair),
   titles as spelled, stop times, subtitles, descriptions, the extra fields (category,
   rating), and every field at once. Units collapse to schedules on (source, file id read) through
   channel_groups.collapse_schedules() with the exact hash as the signature, so an
   alternate source's rows and an active one's land on the same schedule, and units
   reading one id that somehow differ are split and marked.
3. **A cluster is equal `programs` hashes** - the same programs at the same times over
   everything held. Two schedules in a cluster differ only in what the other hashes name,
   which is the detail a person chooses on: one carries the providers' title marks
   (ᴺᵉʷ, ᴸᶦᵛᵉ), one has the longer descriptions, one has subtitles.
4. **Links between clusters** come from a token index: every (comparison title, start
   minute) names the schedules listing it, so the pairs sharing programs are counted
   without comparing everything to everything (4.2 million increments against 198 million
   pairs). A pair sharing at least `MIN_SHARED` of the smaller side's programs is handed
   to compare_listings(), the one judge of "the same program", whose verdict and counts
   are stored unless it says unrelated or nothing overlaps. This is how two copies that
   agree where both list - one stopping at 24 hours, one at 7 days - are found.

Read-only over the guide: nothing here writes a key, an override, an alert or an event
(dev/changelog/1155). A run takes an admission ticket (KIND_EPG_SCAN: it reads what a sync
and EPG maintenance rewrite) and keeps its rows in epg_scans / epg_scan_schedules /
epg_scan_schedule_channels / epg_scan_links, replacing the previous run's in the commit
that marks it finished, so a page always reads one whole result.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import NamedTuple

from . import admission, db
from .channel_groups import collapse_schedules
from .database import (EPGEntry, EpgAlternateEntry, EpgScan, EpgScanLink, EpgScanSchedule,
                       EpgScanScheduleChannel, Channel, EPG_SCAN_FAILED, EPG_SCAN_RUNNING,
                       EPG_SCAN_SUCCESS)
from .db_utils import chunked, retry_on_locked
from .epg_sources import (CMP_NO_OVERLAP, CMP_UNRELATED, Listing, case_sensitive_matching,
                          compare_listings, comparison_title, keys_read_into)
from .program_titles import strip_marks

log = logging.getLogger(__name__)

#: A pair of schedules in different clusters is judged by compare_listings() when the
#: programs they share are at least this share of the smaller side's. Below it the
#: comparator would say unrelated anyway (fewer than half paired), so the work is skipped.
MIN_SHARED = 0.5

#: Rows per streamed fetch and per bulk insert.
_BATCH = 2000

_SEP = b'\x1f'
_END = b'\x1e'


def _digest(text) -> bytes:
    return hashlib.blake2b((text or '').encode('utf-8', 'surrogatepass'), digest_size=8).digest()


class Facets(NamedTuple):
    """The seven fingerprints of one schedule, as hex. Equal `programs` is the cluster;
    equal `exact` is identical in every way; the five between name what differs."""
    programs: str
    titles: str
    stops: str
    subtitles: str
    descriptions: str
    extra: str
    exact: str


class Stats(NamedTuple):
    listings: int
    until: datetime | None
    described: int
    desc_chars: int
    subtitled: int
    marked: int


class _Titles:
    """The per-title values every listing of that title shares, computed once: the
    interned title, its comparison title and whether it carries a provider mark. 49,000
    distinct titles stand behind 572,000 listings on prod, so this is most of the
    fingerprinting work saved."""
    __slots__ = ('seen',)

    def __init__(self):
        self.seen: dict = {}

    def get(self, title):
        r = self.seen.get(title)
        if r is None:
            r = self.seen[title] = (title, comparison_title(title), strip_marks(title) != title)
        return r

    def ctitle(self, title):
        return self.get(title)[1]


class _Unit:
    """One (channel, source)'s listings as the scan holds them. Rows arrive in time
    order (the read sorts them) and go straight into the seven hashers; what is kept per
    listing is what compare_listings() reads of a Listing - the title, the times and
    whether a description exists, carried by a one-character stand-in so no description
    text is held."""
    __slots__ = ('h', 'listings', 'described', 'desc_chars', 'subtitled', 'marked', 'until')

    def __init__(self):
        self.h = [hashlib.blake2b(digest_size=16) for _ in range(7)]
        self.listings = []
        self.described = self.desc_chars = self.subtitled = self.marked = 0
        self.until = None

    def add(self, title, sub_title, description, category, rating, start, stop, titles):
        title, ctitle, is_marked = titles.get(title)
        desc = description or ''
        when = start.isoformat().encode()
        ends = stop.isoformat().encode()
        t = title.encode('utf-8', 'surrogatepass')
        sub_d = _digest(sub_title)
        desc_d = _digest(desc)
        extra_d = _digest(f'{category or ""}\x1f{rating or ""}')
        h = self.h
        h[0].update(ctitle.encode('utf-8', 'surrogatepass') + _SEP + when[:16] + _END)
        h[1].update(t + _SEP + when + _END)
        h[2].update(when + _SEP + ends + _END)
        h[3].update(sub_d + when + _END)
        h[4].update(desc_d + when + _END)
        h[5].update(extra_d + when + _END)
        h[6].update(t + _SEP + sub_d + desc_d + extra_d + when + _SEP + ends + _END)
        if desc:
            self.described += 1
            self.desc_chars += len(desc)
        if sub_title:
            self.subtitled += 1
        if is_marked:
            self.marked += 1
        if self.until is None or stop > self.until:
            self.until = stop
        self.listings.append(Listing(title, None, 'x' if desc else None, start, stop))

    def finish(self) -> tuple[Facets, Stats, list]:
        facets = Facets(*(x.hexdigest() for x in self.h))
        stats = Stats(len(self.listings), self.until, self.described, self.desc_chars,
                      self.subtitled, self.marked)
        self.h = None
        return facets, stats, self.listings


class _Chan(NamedTuple):
    """What keys_read_into() reads of a Channel, for every channel the scan saw, loaded in
    one query rather than 27,000 ORM rows."""
    id: int
    epg_channel_id: str | None


class ScheduleResult(NamedTuple):
    source_id: int | None
    epg_id: str
    spellings: list
    cluster: int
    active: bool
    channel_ids: list
    split: bool
    facets: Facets
    stats: Stats


class LinkResult(NamedTuple):
    a: int          # index into ScanResult.schedules
    b: int
    verdict: str
    compared: int
    same: int
    moved: int
    different: int
    only_a: int
    only_b: int
    description_gaps: int
    largest_move_min: int | None


class ScanResult(NamedTuple):
    listings_as_of: datetime
    listings: int
    schedules: list      # [ScheduleResult]
    links: list          # [LinkResult]
    timings: dict        # step -> seconds, for the log line and the changelog

    @property
    def clusters(self) -> int:
        return sum(1 for n in Counter(s.cluster for s in self.schedules).values() if n > 1)

    @property
    def identical(self) -> int:
        """Clusters of two or more whose every schedule is identical in every way - the
        page's "Identical in every way" tab counts the same thing."""
        exact: dict = defaultdict(set)
        for s in self.schedules:
            exact[s.cluster].add(s.facets.exact)
        counts = Counter(s.cluster for s in self.schedules)
        return sum(1 for c, n in counts.items() if n > 1 and len(exact[c]) == 1)


# ── The pure part: from listings to schedules, clusters and links ─────────────────

def _read_units(now, titles):
    """{(channel id, source id): (Facets, Stats, [Listing])} over both tables, listings
    not yet over at `now`, streamed in (channel, source, time) order so each unit is
    hashed as its rows pass and closed when the next unit starts. Also the row count."""
    finished: dict = {}
    n = 0
    for model in (EPGEntry, EpgAlternateEntry):
        q = (db.session.query(model.channel_id, model.source_id, model.title, model.sub_title,
                              model.description, model.category, model.rating,
                              model.start_time, model.stop_time)
             .filter(model.stop_time > now)
             .order_by(model.channel_id, model.source_id, model.start_time, model.stop_time,
                       model.title)
             .yield_per(_BATCH))
        key = None
        unit = None
        for cid, sid, title, sub, desc, cat, rating, start, stop in q:
            if (cid, sid) != key:
                if unit is not None:
                    finished[key] = unit.finish()
                key = (cid, sid)
                # A (channel, source) in both tables cannot happen - the import moves rows
                # between them - but if it did, the two halves would be hashed apart, so
                # the second is folded into the first rather than overwriting it.
                unit = _Unit() if key not in finished else None
                if unit is None:
                    log.warning('EPG similarity scan: channel %s source %s has rows in both '
                                'listings tables; the second table\'s are skipped', cid, sid)
            if unit is None:
                continue
            unit.add(title, sub, desc, cat, rating, start, stop, titles)
            n += 1
        if unit is not None:
            finished[key] = unit.finish()
    return finished, n


def _find_links(schedules, listings_of, ctitle):
    """Pairs of schedules in different clusters that compare_listings() calls related,
    found through the token index described in the module docstring."""
    index: dict = defaultdict(list)
    tokens_of = []
    for i, s in enumerate(schedules):
        toks = {(ctitle(x.title), x.start.replace(second=0, microsecond=0))
                for x in listings_of[i]}
        tokens_of.append(toks)
        for tok in toks:
            index[tok].append(i)
    links = []
    # Counted one schedule at a time against the ones after it, so what is held is the
    # partners of one schedule rather than every pair in the guide at once - the pair
    # table was most of the scan's memory (dev/changelog/1325).
    for a, toks in enumerate(tokens_of):
        shared: Counter = Counter()
        for tok in toks:
            for b in index[tok]:
                if b > a:
                    shared[b] += 1
        for b, n in shared.items():
            if schedules[a].cluster == schedules[b].cluster:
                continue
            if n < MIN_SHARED * min(len(toks), len(tokens_of[b])):
                continue
            cmp = compare_listings(listings_of[a], listings_of[b])
            if cmp.verdict in (CMP_UNRELATED, CMP_NO_OVERLAP):
                continue
            links.append(LinkResult(
                a, b, cmp.verdict, cmp.compared, cmp.same, cmp.moved, cmp.different,
                cmp.only_active, cmp.only_other, cmp.description_gaps,
                int(cmp.largest_move.total_seconds() // 60) if cmp.largest_move else None))
    return links


def scan_listings(now=None, cfg=None) -> ScanResult:
    """The whole scan, over rows read here and nothing written. `cfg` is the loaded
    config (for the case rule); `now` is the instant listings are read from."""
    from .config import load_config
    now = now or datetime.utcnow()
    cfg = cfg if cfg is not None else load_config()
    timings = {}
    t = time.perf_counter()
    titles = _Titles()
    finished, n_rows = _read_units(now, titles)
    timings['read'] = time.perf_counter() - t

    t = time.perf_counter()
    cids = sorted({cid for cid, _sid in finished})
    chans = {}
    for piece in chunked(cids, 500):
        for cid, eid in db.session.query(Channel.id, Channel.epg_channel_id).filter(
                Channel.id.in_(piece)):
            chans[cid] = _Chan(cid, eid)
    active_of = {}
    for piece in chunked(cids, 500):
        for cid, sid in db.session.query(Channel.id, Channel.epg_source_id).filter(
                Channel.id.in_(piece)):
            active_of[cid] = sid
    cs = case_sensitive_matching(cfg)
    reads = keys_read_into([(chans[cid], sid) for cid, sid in finished if cid in chans], cs)
    collapsed = collapse_schedules(
        [u for u in finished if u[0] in chans], reads.get, lambda u: finished[u][0].exact, cs)
    timings['collapse'] = time.perf_counter() - t

    t = time.perf_counter()
    cluster_of: dict = {}
    schedules = []
    listings_of = []
    for s in collapsed:
        facets, stats, listings = finished[s.channels[0]]
        cluster = cluster_of.setdefault(facets.programs, len(cluster_of))
        channel_ids = sorted({cid for cid, _sid in s.channels})
        schedules.append(ScheduleResult(
            s.source_id, s.file_id, list(s.spellings), cluster,
            any(active_of.get(cid) == sid for cid, sid in s.channels),
            channel_ids, s.split, facets, stats))
        listings_of.append(listings)
    timings['cluster'] = time.perf_counter() - t

    t = time.perf_counter()
    links = _find_links(schedules, listings_of, titles.ctitle)
    timings['links'] = time.perf_counter() - t
    return ScanResult(now, n_rows, schedules, links, timings)


# ── The run: admission, the rows, the thread ─────────────────────────────────────

def _persist(scan_id: int, result: ScanResult) -> None:
    """Write the result and mark the scan SUCCESS in one commit, dropping every other
    scan's rows in the same unit. Re-runnable from the top: it deletes this scan's own
    rows first, so a lock retry cannot double them."""
    @retry_on_locked()
    def _write():
        for model in (EpgScanLink, EpgScanScheduleChannel, EpgScanSchedule):
            model.query.filter(model.scan_id == scan_id).delete(synchronize_session=False)
        ids = []
        for piece in chunked(result.schedules, _BATCH):
            objs = [EpgScanSchedule(
                scan_id=scan_id, source_id=s.source_id, epg_id=s.epg_id,
                spellings=json.dumps(s.spellings), cluster=s.cluster, active=s.active,
                channel_count=len(s.channel_ids), listings=s.stats.listings,
                until=s.stats.until, described=s.stats.described,
                desc_chars=s.stats.desc_chars, subtitled=s.stats.subtitled,
                marked=s.stats.marked, h_programs=s.facets.programs,
                h_titles=s.facets.titles, h_stops=s.facets.stops,
                h_subtitles=s.facets.subtitles,
                h_descriptions=s.facets.descriptions, h_extra=s.facets.extra,
                h_exact=s.facets.exact) for s in piece]
            db.session.add_all(objs)
            db.session.flush()
            ids.extend(o.id for o in objs)
        chan_rows = [{'scan_id': scan_id, 'schedule_id': ids[i], 'channel_id': cid}
                     for i, s in enumerate(result.schedules) for cid in s.channel_ids]
        for piece in chunked(chan_rows, _BATCH):
            db.session.bulk_insert_mappings(EpgScanScheduleChannel, piece)
        link_rows = [{'scan_id': scan_id, 'a_id': ids[x.a], 'b_id': ids[x.b],
                      'verdict': x.verdict, 'compared': x.compared, 'same': x.same,
                      'moved': x.moved, 'different': x.different, 'only_a': x.only_a,
                      'only_b': x.only_b, 'description_gaps': x.description_gaps,
                      'largest_move_min': x.largest_move_min} for x in result.links]
        for piece in chunked(link_rows, _BATCH):
            db.session.bulk_insert_mappings(EpgScanLink, piece)
        scan = db.session.get(EpgScan, scan_id)
        scan.status = EPG_SCAN_SUCCESS
        scan.finished_at = datetime.utcnow()
        scan.listings_as_of = result.listings_as_of
        scan.listings = result.listings
        scan.schedules = len(result.schedules)
        scan.clusters = result.clusters
        scan.identical = result.identical
        scan.links = len(result.links)
        scan.elapsed_ms = int((scan.finished_at - scan.started_at).total_seconds() * 1000)
        others = [s.id for s in EpgScan.query.filter(EpgScan.id != scan_id)]
        for model in (EpgScanLink, EpgScanScheduleChannel, EpgScanSchedule):
            model.query.filter(model.scan_id.in_(others)).delete(synchronize_session=False)
        EpgScan.query.filter(EpgScan.id.in_(others)).delete(synchronize_session=False)
        db.session.commit()
    _write()


def _fail(scan_id: int, error: str) -> None:
    @retry_on_locked()
    def _write():
        scan = db.session.get(EpgScan, scan_id)
        if scan is not None:
            scan.status = EPG_SCAN_FAILED
            scan.finished_at = datetime.utcnow()
            scan.error = error
            db.session.commit()
    _write()


def run_scan(scan_id: int, ticket) -> None:
    """The body of a scan whose row exists and whose ticket is granted: compute, persist,
    and on any failure mark the row FAILED with the reason. The ticket is released on
    every path. Takes the DB write for the result; its own callers never write."""
    try:
        result = scan_listings()
        _persist(scan_id, result)
        log.info('EPG similarity scan %d: %d listings, %d schedules, %d clusters, %d links; '
                 '%s', scan_id, result.listings, len(result.schedules), result.clusters,
                 len(result.links),
                 ', '.join(f'{k} {v:.1f}s' for k, v in result.timings.items()))
    except Exception as exc:
        log.exception('EPG similarity scan %d failed', scan_id)
        db.session.rollback()
        try:
            _fail(scan_id, f'{type(exc).__name__}: {exc}')
        except Exception:
            log.exception('EPG similarity scan %d: could not record the failure', scan_id)
    finally:
        admission.release(ticket)


def start_scan(app, label: str = 'EPG Manager'):
    """Start a scan on its own thread: (EpgScan, None) when it started, (None, reason)
    when admission refused it. The row is committed RUNNING before the thread starts, so
    a process that dies leaves a row the next startup can name."""
    ticket = admission.try_start(admission.KIND_EPG_SCAN, label)
    if not ticket.granted:
        return None, ticket.reason

    @retry_on_locked()
    def _create():
        scan = EpgScan(started_at=datetime.utcnow(), status=EPG_SCAN_RUNNING)
        db.session.add(scan)
        db.session.commit()
        return scan.id

    try:
        scan_id = _create()
    except Exception:
        admission.release(ticket)
        raise

    def body():
        with app.app_context():
            run_scan(scan_id, ticket)

    threading.Thread(target=body, name=f'epg-scan-{scan_id}', daemon=True).start()
    return db.session.get(EpgScan, scan_id), None


def reconcile_interrupted_scans() -> int:
    """Mark RUNNING scans FAILED at startup: nothing in this process has started one yet,
    so a RUNNING row belongs to a process that is gone, and left alone it would read as a
    scan still running forever. Returns how many it marked."""
    @retry_on_locked()
    def _mark():
        stranded = EpgScan.query.filter_by(status=EPG_SCAN_RUNNING).all()
        for scan in stranded:
            log.warning('EPG similarity scan %d was left RUNNING at %s by a process that is '
                        'gone - marking it FAILED', scan.id, scan.started_at)
            scan.status = EPG_SCAN_FAILED
            scan.finished_at = datetime.utcnow()
            scan.error = 'Interrupted by a restart before it finished.'
        if stranded:
            db.session.commit()
        return len(stranded)
    return _mark()


def latest_scan():
    """The most recent scan row of any status, or None."""
    return EpgScan.query.order_by(EpgScan.id.desc()).first()


def latest_result():
    """The most recent SUCCESS scan - the one whose rows the pages read - or None."""
    return (EpgScan.query.filter_by(status=EPG_SCAN_SUCCESS)
            .order_by(EpgScan.id.desc()).first())
