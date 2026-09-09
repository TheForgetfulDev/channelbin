"""The channel search's JSON surface: the debounced paged fetch, and the catalog it needs.

Split by how fast the answer changes, and by whether the page can proceed without it:

* **`GET /api/channels/search`** is re-run on every keystroke (F1: a debounced *paged server
  fetch*, settled 2026-07-27 - client-side filtering of the loaded page was rejected outright
  because it can only ever see 100 of 136,130 channels, so its counts would be a guess). It
  carries the ROWS, which nothing substitutes for, plus whether the search ran degraded.
* **`GET /api/channels/search/counts`** and **`GET /api/channels/search/facets`** carry the
  numbers *around* the rows - the total and per-standing-option hidden counts, and the facet
  rail. Separate because they are optional in a way the rows are not: they arrive beside rows
  already on screen, so while the index is unusable they are attempted cheaply and reported
  as `declined` rather than allowed to hold up (or fail) the answer. See
  `_optional_aggregate`, and dev/changelog/598 and 675 for the two halves of that split.
* **`GET /api/channels/search/catalog`** is fetched once per page load. It carries what does
  not depend on the search: the registries (fields, dimensions, standing options, sorts) and
  the vocabularies (accounts, tags, groups, categories). Sending 1,728 category names back on
  every keystroke would be most of the response for data that had not changed.

**The registries are served, not duplicated.** Everything the rail and the Search-in menu
render comes out of `app/channel_search.py`'s own tuples, so adding a search field or a facet
stays a one-entry change in the engine rather than an edit in three files that drift.

**`facets=` is the lever this endpoint is built around.** Absent, every visible dimension is
counted (667ms on the production database at the default unfiltered load); present-but-empty,
none are (46ms). So the page can ask for rows on each keystroke and let the rail catch up
behind, and a dimension missing from `facets` means "not requested" - which the UI must
render as such and never as zero.

Errors: a state this engine cannot honour - an unknown field, dimension, sort or grain - is a
400 naming the problem, never a silently different search. That is the whole point of the
registry: a typo in a link is an error its author sees, not an empty result they debug. A
request that outruns its time budget is a 503 naming why (see `SEARCH TIME BUDGET` below);
either way the failure is a sentence the user can read, never a short result they cannot tell
apart from a real one.
"""
import logging
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode

from flask import Blueprint, jsonify, request

from .. import db, health_bands
from ..channel_search import (DEFAULT_FIELDS, DEFAULT_PAGE_SIZE, DEFAULT_SORT,
                              DEFAULT_SORT_BY_GRAIN, DEFAULT_STANDING, DIMENSIONS,
                              FIELD_GROUPS, FIELDS, GRAIN_CHANNELS,
                              GROUP_ANY, HEALTH_UNTESTED,
                              HEALTH_UNTESTED_LABEL, IMPLEMENTED_GRAINS, MAX_PAGE_SIZE,
                              OTHER_LABELS, OTHER_NEW, OTHER_VALUES, SORTS, SORTS_BY_GRAIN,
                              STANDING_OPTIONS, WHEN_STATIC_LABELS, WHEN_STATIC_VALUES,
                              SearchContext, SearchState, SearchStateError,
                              default_fields_for, default_standing_for,
                              dimensions_for, full_scan_reason,
                              probe_degraded_reason, search, search_counts,
                              search_facets, standing_options_for)
from ..channel_search_rows import build_rows
from ..config import load_config
from ..database import Account, Channel, ChannelGroup, ChannelGroupMember, Tag
from ..db_utils import (ConcurrencyGate, ConcurrencyGateTimeout, QueryAbandoned,
                        QueryDeadlineExceeded, query_deadline)

log = logging.getLogger(__name__)

channel_search_bp = Blueprint('channel_search', __name__)

#: Process-wide, deliberately: what it protects is this box's two cores, not one app
#: instance's state. Only searches running WITHOUT their index queue here - see
#: `search.max_concurrent_unindexed` in app/config.py for the cap and the reasoning.
UNINDEXED_SCANS = ConcurrencyGate('unindexed search scan')

#: How much of a request's time budget may be spent QUEUING for the scan slot, leaving the
#: rest as runway to actually run in.
#:
#: Without a reserve the wait timeout was the whole budget, so a request could be handed the
#: slot with a rounding error left, arm a deadline of that size, abort on its first statement
#: and report a timeout - blaming the query for what was starvation. Measured live during a
#: real index rebuild: two searches 0.25s apart, and the second logged
#: `Search timed out after 0.228517s of a 15s budget` having done no work at all
#: (dev/changelog/678).
#:
#: A fifth of the budget rather than a measured constant: it scales with whatever an operator
#: sets `search.degraded_timeout_seconds` to, and has no degenerate case at small budgets. It
#: is not a completion guarantee - one degraded airing scan costs ~11.5s of the default 15s
#: while the rebuild that caused the degradation is running - it is the line past which taking
#: the slot can only produce a failure more slowly than refusing it.
SLOT_WAIT_BUDGET_FRACTION = 0.8

#: The two request lanes a search page runs at once, each with its own counter on the page
#: (`rowSeq` and `facetSeq` in static/js/channel-search.js). `/counts` shares the row lane
#: because the page sequences it against the row request it describes, not against itself.
LANE_ROWS = 'rows'
LANE_FACETS = 'facets'


class SupersessionRegistry:
    """Which request each search page is still waiting for an answer to.

    The page aborts a superseded keystroke's request, but an abort is invisible to a WSGI
    server: Werkzeug runs the handler thread to completion and notices the dead peer only
    when it writes the response. So during a degraded window the single scan slot was spent,
    in full, on searches nobody was waiting for, while the one the user was watching queued
    behind them. Measured live with a rebuild running: three searches 0.25s apart, and the
    FIRST one - already abandoned by the page - got the only complete answer while both
    later ones 503'd (dev/changelog/678).

    Nothing at the socket level can close that, so the page says it out loud instead. Every
    search request carries `sid` (one per page load) and `seq` (that page's own request
    counter, which it already keeps for its stale-answer guard). A strictly newer seq on the
    same (sid, lane) means the page has moved on, and whoever is holding the slot can stop.

    **Absent parameters mean today's behavior**, never an error: this URL contract is an API
    other pages link into, and a caller that does not track its own requests simply never
    gets superseded.

    Bounded, and eviction is safe in the conservative direction: past MAX_SESSIONS the least
    recently used pairs fall out, and a request whose entry has been evicted reads as "not
    superseded" - it runs to completion exactly as it does today.
    """

    #: Page loads x lanes to remember. Two per open search page, so this is three orders of
    #: magnitude above what a single-user install reaches; the cap exists so a long-lived
    #: process cannot accumulate one entry per page load forever.
    MAX_SESSIONS = 256

    def __init__(self):
        self._lock = threading.Lock()
        self._latest = {}

    def arrive(self, sid, seq, lane):
        """Record this request and return a predicate for "has it been superseded since".

        `None` for a caller that supplied no id - not a predicate that always answers False.
        The difference is visible: `None` reaches `query_deadline` as "install no
        abandonment check", so a caller outside this page pays literally nothing for a
        mechanism it does not use.
        """
        if not sid or seq is None:
            return None
        key = (sid, lane)
        latest = self._latest
        with self._lock:
            if seq > latest.get(key, 0):
                # Re-inserted rather than assigned, so plain dict insertion order IS recency
                # order and the eviction below drops the least recently used pair.
                latest.pop(key, None)
                latest[key] = seq
            while len(latest) > self.MAX_SESSIONS:
                latest.pop(next(iter(latest)))
        # Read WITHOUT the lock, deliberately: this predicate is polled from inside a SQLite
        # progress handler every 20,000 VDBE ops, and a dict lookup is atomic under the GIL.
        # Taking a lock there would put a running scan behind every other request's arrival.
        return lambda: latest.get(key, seq) > seq

    def reset(self):
        """Forget every session. For tests - see tests/support/app.py."""
        with self._lock:
            self._latest.clear()


#: Process-wide for the same reason UNINDEXED_SCANS is: the work being cancelled is a thread
#: on this box, not per-app state.
SEARCH_GENERATIONS = SupersessionRegistry()


def _supersession(lane):
    """This request's abandonment predicate, or None when the caller sent no id.

    A malformed `seq` is treated as absent rather than as a 400: this parameter is a
    performance hint about the CALLER's own bookkeeping, not part of the search it is
    asking for, and refusing to search because of it would trade a slow answer for no
    answer. Everything that describes the search itself is still strict (SearchState).
    """
    sid = request.args.get('sid', '')[:64]
    try:
        seq = int(request.args.get('seq', ''))
    except (TypeError, ValueError):
        return None
    return SEARCH_GENERATIONS.arrive(sid, seq, lane)


@contextmanager
def _search_slot(cfg, degraded, budget, state, wait_for_slot=True, abandoned=None,
                 report=None):
    """The concurrency slot + time budget + built `SearchContext` every search-engine entry
    point shares (dev/changelog/418, 422) - now two of them (`/search` and `/search/counts`,
    dev/changelog/598), since a counts-only query can be exactly as slow as the row query's
    own breakdown call when the index is mid-rebuild and deserves the identical protection,
    not a smaller version of it.

    `degraded`/`budget` are passed in rather than computed here so they stay available to the
    caller's own `except` blocks even when the exception fires before this generator reaches
    its `yield` (`ConcurrencyGateTimeout` from the gate itself) - a value only bound via `as`
    would not be.

    `wait_for_slot=False` means "take a slot only if one is free right now, otherwise give
    up immediately" - `timeout=0`, which `Condition.wait_for` evaluates once and returns
    False on. That is what an OPTIONAL aggregate passes (dev/changelog/676): a number that is
    allowed to be declined must never sit in the queue ahead of the rows someone is waiting
    on. Note it has to bypass the `budget or None` spelling below, which would otherwise read
    a literal 0 as "wait forever" - the exact opposite.

    `abandoned` is the caller's supersession predicate. It is checked at BOTH ends of the
    slot - the instant one is acquired, and continuously while the query runs - so a request
    the page has moved on from neither occupies the slot nor keeps scanning for an answer
    nobody will read (dev/changelog/678). Checked here rather than only before the wait
    because the interesting case is precisely a request that was live when it queued.

    `report` is an optional dict this fills in with how the time was spent, so the caller's
    `except` blocks can say which part of the budget went where. Same reason `degraded` and
    `budget` are passed in rather than computed here: an exception can fire before this
    generator reaches its `yield`, and a value bound via `as` would not survive that.
    """
    started = time.monotonic()
    # SCAN CAP, then TIME BUDGET, in that order.
    #
    # The cap bounds how many unindexed scans run at once; the budget bounds how long one
    # runs. They are the two halves of the same incident and neither substitutes for the
    # other. Only a degraded request queues - a healthy one passes limit=0 and never touches
    # the condition variable.
    #
    # ONE clock covers both. The wait is charged against the same budget as the query, so the
    # handler is bounded by a single number rather than by wait + budget: someone who queued
    # for 14 of their 15 seconds waited 15 seconds either way, and telling them so after 29
    # would be worse.
    #
    # But NOT the whole budget: SLOT_WAIT_BUDGET_FRACTION reserves the tail as runway, so a
    # request that cannot get a turn in time is shed as one that never started rather than
    # handed the slot with a rounding error left and reported as a timeout.
    wait = (budget * SLOT_WAIT_BUDGET_FRACTION or None) if wait_for_slot else 0
    with UNINDEXED_SCANS.slot(cfg['search']['max_concurrent_unindexed'] if degraded else 0,
                              timeout=wait):
        waited = time.monotonic() - started
        if report is not None:
            report['waited'] = waited
        # Before opening a connection, let alone building a query: a request whose page moved
        # on while it queued gives the slot straight back to the next waiter.
        if abandoned is not None and abandoned():
            raise QueryAbandoned(f'{state.grain} search {state.q!r}')
        # Inside the gate deliberately: this is the first thing that opens a connection. One
        # context per request, before any query is built - it is what makes the engine's "no
        # hidden I/O in per-row loops" rule structural rather than a promise.
        ctx = SearchContext.build(cfg)
        # Every request, not only the ones expected to be slow: this is the one choke point
        # both grains and every entry point come through, so a blanket budget here cannot be
        # outflanked by a slow path nobody anticipated. Which budget and why: app/config.py's
        # `search` defaults.
        #
        # Clamped above zero rather than allowed to reach it: query_deadline reads a
        # non-positive value as "no deadline", so a request that spent its whole budget
        # queuing would come out the other side unbounded - the one case this most needs to
        # bound.
        remaining = max(budget - waited, 1e-6) if budget else 0
        with query_deadline(remaining, label=f'{state.grain} search {state.q!r}',
                            abandoned=abandoned):
            yield ctx


@channel_search_bp.route('/api/channels/search')
def channel_search_api():
    try:
        state = SearchState.from_params(request.args)
    except SearchStateError as exc:
        return jsonify({'error': str(exc)}), 400
    # Skips `_standing_breakdown()` (~1.6s of the airing grain's unfiltered ~2.1s, all of it
    # spent on a query the row page never needed - dev/changelog/598): `total`/`pages`/
    # `standing_hidden` come back `None` (pending, not zero) and the page fetches them
    # separately from GET /search/counts once it wants them. Default true (compute inline)
    # keeps every existing caller - other pages linking in, the tests - byte-identical.
    want_counts = request.args.get('counts', '1') != '0'
    want_facets = True

    # FIRST, before the probe and well before the queue: registering this request's seq is
    # what lets the requests already queued or scanning for this page find out they have
    # been superseded. Register it late and they keep the slot for exactly as long as the
    # registration is delayed.
    abandoned = _supersession(LANE_ROWS)

    cfg = load_config()
    # Resolved FIRST, and without a built context, because it decides three things: whether
    # this request queues at all, which budget it gets, and what a failure says. Probing it
    # here rather than off `ctx` is what lets the queue below be made of threads instead of
    # database connections - see probe_degraded_reason().
    degraded = probe_degraded_reason(state)
    # The other reason the numbers are expensive, and it is not the same reason: the index is
    # healthy but cannot answer what was asked, so every aggregate reads the whole table
    # (dev/changelog/681). Pure state, so it costs nothing and needs no connection either.
    full_scan = full_scan_reason(state)
    # Hand the probe's connection back before anything can block. A thread waiting on the
    # gate while holding a pooled connection would relocate the exhaustion this whole batch
    # exists to end, not fix it. Safe unconditionally: everything above is read-only.
    db.session.rollback()

    # ROWS FIRST, ALWAYS. When the numbers around them are expensive this request answers the
    # only question that has no substitute - which rows match - and every number is dropped
    # here regardless of what the caller asked for (dev/changelog/676).
    #
    # It is dropped rather than attempted-and-abandoned because this request holds the scan
    # slot: a total that blows its budget inside here has still spent that budget on the one
    # request the user is watching. The aggregate endpoints below attempt it instead, off the
    # critical path, where being declined costs nothing.
    #
    # Measured, on the live database: an unindexed airing keystroke was ~14.6s of CPU through
    # one slot (3.4s page query + 3.6s COUNT + a rail request that re-ran the page query and
    # added a 3.8s grouped scan). This leaves the 3.4s that is actually load-bearing.
    #
    # The full-scan case reaches the same conclusion from the opposite direction and was the
    # last dead end left on this endpoint. A bare `?grain=airings` with the past included
    # spent 20s building a 24.4s rail and 503'd - throwing away a 0.1s row page for numbers
    # nobody was owed. The page itself never hit it (it sends `counts=0&facets=`), but every
    # other caller of this URL did.
    declined = []
    declined_reason = ''
    if degraded or full_scan:
        want_counts = False
        want_facets = False
        declined = ['counts', 'facets']
        # Degraded first when both hold: it is the one that will clear on its own, so it is
        # the one whose remedy ("wait") is worth naming over the one that will not.
        declined_reason = degraded or full_scan

    budget = cfg['search']['degraded_timeout_seconds' if degraded else 'timeout_seconds']
    timing = {}
    try:
        with _search_slot(cfg, degraded, budget, state, abandoned=abandoned,
                          report=timing) as ctx:
            # The row build is inside the slot deliberately - its enrichment queries run on
            # the same starved box.
            result = search(state, ctx, want_counts=want_counts, want_facets=want_facets)
            rows = build_rows(result, state, ctx)
    except SearchStateError as exc:
        return jsonify({'error': str(exc)}), 400
    except QueryAbandoned:
        return _superseded(state, degraded, LANE_ROWS)
    except ConcurrencyGateTimeout as exc:
        return _too_busy(state, degraded, exc, budget)
    except QueryDeadlineExceeded as exc:
        return _timed_out(state, degraded, exc, budget, timing.get('waited', 0.0))

    return jsonify({
        'success': True,
        'grain': state.grain,
        'rows': rows,
        'total': result.total,
        # The two numbers behind `total`, never one blended one
        # (DESIGN-group-search-rows.md §5.2). `total` is their sum because that is what the
        # pager pages; the results heading names both kinds, and the facet rail beside them
        # counts channels and nothing else - which the count line says out loud rather than
        # leaving the reader to notice the rail and the heading disagreeing.
        'channel_total': result.channel_total,
        'group_total': result.group_total,
        'page': result.page,
        'page_size': result.page_size,
        'pages': result.pages,
        # Per option, never a merged total: a lump sum says something vanished but not what
        # to do about it, and each of these is a control the user can switch off by name.
        # `None` (JSON null) when `want_counts` was false - pending, not a real zero.
        'standing_hidden': result.standing_hidden,
        'facets': result.facets,
        # Which dimensions were actually counted. Absent from `facets` means "not asked
        # for", and the rail has to be able to tell that from a genuine zero.
        'facets_counted': sorted(result.facets),
        # What this response deliberately did NOT answer, so the page can say so instead of
        # rendering a pending spinner forever or a `--` the reader has to guess at. Always
        # present, empty when nothing was declined: absent-vs-empty is a distinction this
        # page has already been bitten by once (`facets`), and one is enough.
        #
        'declined': declined,
        # WHY they were declined, in one sentence the page can render as-is. `degraded` alone
        # used to carry this, which worked only while "the index is unusable" was the only
        # way a number could be dropped - the page hardcoded that wording in two places. A
        # healthy search the index cannot answer is a different sentence with a different
        # remedy, so the server spells it and the page renders whatever arrives. Empty when
        # nothing was declined.
        'declined_reason': declined_reason,
        'degraded': result.degraded,
        # The state as this engine understood it, so the page can put it in the address bar
        # and other surfaces can link straight back into the same search.
        'query_string': urlencode(state.to_params()),
    })


def _optional_aggregate(kind, state, run, when_declined, lane):
    """Run an aggregate that the page can live without: full protection when it is cheap,
    cheap-or-declined when it is not (dev/changelog/676, 681).

    The two aggregate-only endpoints (`/counts`, `/facets`) share this because they are the
    same kind of request - a number that arrives beside rows already on screen. That makes a
    failure here categorically different from a failure of the row endpoint, and the
    difference is the whole point:

    * **Ordinary:** unchanged. Full `timeout_seconds`, the ordinary slot, and a 503 on either
      failure - a caller that asked for a total and cannot have one is owed an error.
    * **Expensive:** a much shorter budget, and either failure is a **200 carrying
      `declined`**, not a 503. The rows are already correct and on screen, so this is an
      incomplete answer rather than a failed request, and reporting it as a failure would put
      an error toast over a page that is working.

    TWO DIFFERENT THINGS MAKE IT EXPENSIVE, and they are not interchangeable:

    * **Degraded** - the index is unusable right now (a rebuild, a sync). Temporary, repairs
      itself within minutes (dev/changelog/680), and the remedy is to wait. The slot is taken
      NON-BLOCKING here: a number that is allowed to be declined must never sit in the queue
      ahead of the rows someone is waiting on.
    * **Full scan** - the index is healthy and simply cannot answer this question, so the
      aggregate reads the whole table (`full_scan_reason`). It never repairs; the remedy is a
      control on the page. No non-blocking slot, because a healthy request never queues for
      one in the first place (`_search_slot` passes limit=0), and forcing one would only
      change behavior in the case where BOTH hold - which degraded already owns below.

    ATTEMPTED, NOT REFUSED, and the test suite is one of the reasons. "Never built" is one of
    the four states that make a search degraded, so a fresh install - and every
    `make_test_app()` that never rebuilds its indexes - is permanently degraded. A blanket
    "degraded means no totals" would leave a 300-channel install with no numbers forever, for
    aggregates costing microseconds there. The same holds for the full-scan case: reading
    every row of a 300-channel install's EPG is instant. Letting the cost decide is
    self-tuning, and there is nothing else it could be - with the past included the rail
    measures 1.2s to 24.4s in a continuous spread as filters narrow it, so no test on the
    SHAPE of the request could separate the answerable ones from the rest.
    """
    abandoned = _supersession(lane)
    cfg = load_config()
    degraded = probe_degraded_reason(state)
    full_scan = full_scan_reason(state)
    # Same reason the row endpoint does it: never queue while holding a pooled connection.
    db.session.rollback()

    if not (degraded or full_scan):
        budget = cfg['search']['timeout_seconds']
        timing = {}
        try:
            with _search_slot(cfg, degraded, budget, state, abandoned=abandoned,
                              report=timing) as ctx:
                return _answered(run(state, ctx))
        except SearchStateError as exc:
            return jsonify({'error': str(exc)}), 400
        except QueryAbandoned:
            return _superseded(state, degraded, lane)
        except ConcurrencyGateTimeout as exc:
            return _too_busy(state, degraded, exc, budget)
        except QueryDeadlineExceeded as exc:
            return _timed_out(state, degraded, exc, budget, timing.get('waited', 0.0))

    # Degraded wins when both hold: its budget is the tighter of the two, and its slot
    # discipline is the one protecting the rebuild that ends the degraded window.
    if degraded:
        reason, budget = degraded, cfg['search'].get('degraded_aggregate_timeout_seconds', 2)
    else:
        reason = full_scan
        budget = cfg['search'].get('full_scan_aggregate_timeout_seconds', 8)
    declined = {'success': True, 'declined': [kind], 'declined_reason': reason,
                **when_declined}
    # 0 means "never attempt" - an operator's off switch, and the lever a test uses to reach
    # this branch deterministically instead of racing a real timeout.
    if not budget:
        return jsonify(declined)
    try:
        with _search_slot(cfg, degraded, budget, state, wait_for_slot=not degraded,
                          abandoned=abandoned) as ctx:
            return _answered(run(state, ctx))
    except SearchStateError as exc:
        return jsonify({'error': str(exc)}), 400
    except QueryAbandoned:
        # Ahead of the declined branch below on purpose: "the page moved on" and "this was
        # too expensive to answer" are different facts, and only the second one is a decline.
        return _superseded(state, reason, lane)
    except (ConcurrencyGateTimeout, QueryDeadlineExceeded) as exc:
        log.info('Declined the %s aggregate after %gs because %s: %s (grain=%s q=%r)',
                 kind, budget, reason, exc, state.grain, state.q)
        return jsonify(declined)


def _answered(payload):
    """An aggregate that DID come back. `declined_reason` is stated as empty rather than
    omitted, for the same reason `declined` itself always ships: absent-vs-empty is a
    distinction this page has already been bitten by once, and one is enough."""
    return jsonify({'success': True, 'declined': [], 'declined_reason': '', **payload})


@channel_search_bp.route('/api/channels/search/counts')
def channel_search_counts_api():
    """`total`/`pages`/`standing_hidden` alone - the trailing request that fills in what
    `GET /api/channels/search?counts=0` skipped (dev/changelog/598). Same search state, same
    concurrency slot and time budget as the row endpoint when the index is healthy (a
    counts-only query can be exactly as slow when unindexed); optional, cheap-or-declined
    when it is not - see `_optional_aggregate`. Does not build rows or the facet rail.
    """
    try:
        state = SearchState.from_params(request.args)
    except SearchStateError as exc:
        return jsonify({'error': str(exc)}), 400

    def run(state, ctx):
        hidden, total, pages, groups = search_counts(state, ctx)
        return {'total': total, 'pages': pages, 'standing_hidden': hidden,
                # The two numbers behind `total`, never one blended one - the results
                # heading names both kinds, and the rail's facet counts stay channel-only.
                'group_total': groups, 'channel_total': None if total is None else total - groups}

    return _optional_aggregate(
        'counts', state, run,
        {'total': None, 'pages': None, 'standing_hidden': None,
         'group_total': None, 'channel_total': None},
        LANE_ROWS)


@channel_search_bp.route('/api/channels/search/facets')
def channel_search_facets_api():
    """The facet rail alone - symmetric with `/counts` above, and for the same reason.

    The rail used to be fetched from the row endpoint with `facets=<dims>&counts=0`, which
    ran the full LIMIT-100 row query and discarded the rows. Every keystroke therefore paid
    the page query twice: ~3.4s of pure waste per unindexed airing keystroke on the live
    database, and not free on a healthy one either (dev/changelog/676).

    Optional in the same sense `/counts` is: while degraded it takes a non-blocking slot on a
    short budget and answers `declined` rather than 503, so the rail can never be what keeps
    the rows waiting.
    """
    try:
        state = SearchState.from_params(request.args)
    except SearchStateError as exc:
        return jsonify({'error': str(exc)}), 400

    def run(state, ctx):
        facets = search_facets(state, ctx)
        return {'facets': facets, 'facets_counted': sorted(facets)}

    return _optional_aggregate(
        'facets', state, run, {'facets': {}, 'facets_counted': []}, LANE_FACETS)


def _superseded(state, reason, lane):
    """The answer to a request whose own page has already moved on.

    Nobody renders this: the page aborted this request before issuing the newer one, and its
    own sequence guard drops any answer that outruns the abort. It exists so the LOG names
    what actually happened - work correctly cancelled - instead of attributing it to a shed
    or a timeout, and so a caller that does read it gets a sentence rather than an empty 200
    it cannot tell apart from "nothing matched".

    409 rather than 503: nothing failed and nothing is overloaded. This request lost to a
    newer one from the same page, which is a conflict between two requests rather than a
    condition anybody can act on. INFO rather than WARNING for the same reason - this is the
    mechanism working, and logging it at WARNING would bury the two real failures below it
    under the ordinary noise of someone typing.
    """
    log.info('Search superseded by a newer %s request from the same page, work cancelled '
             '(grain=%s q=%r page=%d)%s',
             lane, state.grain, state.q, state.page, f': {reason}' if reason else '')
    return jsonify({'error': (
        'This search was superseded by a newer one from the same page.')}), 409


def _too_busy(state, reason, exc, budget):
    """The 503 for a search that never got a slot: shed out loud, never as an empty result.

    Sheds rather than queues without limit because the thing being protected is the work
    that ENDS the degraded window - the index rebuild and the sync behind it. A request that
    has already waited most of its budget will not get a useful answer by waiting longer; it
    will only keep a thread and, eventually, a connection away from the rebuild.

    503 rather than 429: the box is the constrained resource, not this caller's rate, and it
    keeps every "search could not answer" case on one status the page already handles.

    **"Never started" is the load-bearing half of this message.** The wait is capped below
    the whole budget (SLOT_WAIT_BUDGET_FRACTION) precisely so this case stays distinguishable
    from _timed_out below: before that reserve existed a starved request could be handed the
    slot with a rounding error left and reported as a timeout instead, which named the wrong
    cause and pointed at the wrong fix (dev/changelog/678).
    """
    log.warning('Search shed after waiting %gs of a %gs budget for one of %d scan slots, '
                'never started (grain=%s q=%r page=%d)%s',
                exc.seconds, budget, exc.limit, state.grain, state.q, state.page,
                f': {reason}' if reason else '')
    return jsonify({'error': (
        f'The search index is being rebuilt, so this search has to scan instead - and '
        f'{"another one is" if exc.limit == 1 else f"{exc.limit} of those are"} already '
        f'running. It waited {exc.seconds:g}s for a turn and never started. Results are '
        f'still correct once the rebuild finishes - try again in a minute.')}), 503


def _timed_out(state, reason, exc, budget, waited=0.0):
    """The 503 for a search that outran its budget: loud, and it says why.

    A blown budget is reported as a failure rather than as a short result on purpose. The
    honest alternatives were "fewer rows than exist" and "counts that do not add up", and
    both are indistinguishable on screen from a real answer - the exact silence this
    project's founding principle exists to stop.

    `reason` is the engine's own `degraded_reason()`, not a second sentence written here:
    the page already renders that same string in its `unindexed` tooltip, and two spellings
    of one fact drift.

    `budget` is the request's WHOLE budget, and the message quotes that rather than
    `exc.seconds`. Those differ once a request has queued for a slot: the deadline is armed
    with whatever is left, so `exc.seconds` would report a remainder the reader has no way
    to interpret ("timed out after 3.4s" when the setting says 15).

    `waited` is how much of that budget went to the queue rather than to the query, and it
    is stated whenever it is not negligible. Quoting the budget alone was true but not the
    whole truth: a request that spent four fifths of its budget behind another search and
    then blew the rest is not describable as "your search is too broad", and telling its
    reader so sends them to narrow a query that was never the problem.
    """
    log.warning('Search timed out after %gs of a %gs budget, %gs of it queued for a scan '
                'slot (grain=%s q=%r page=%d facets=%s)%s',
                exc.seconds, budget, waited, state.grain, state.q, state.page,
                'all' if state.facets is None else sorted(state.facets),
                f': {reason}' if reason else '')
    queued = f' {waited:.1f}s of that was spent waiting for a turn behind another search.' \
        if waited >= 0.1 else ''
    budget = f'{budget:g}'
    if reason:
        message = (f'Search timed out after {budget}s.{queued} It is running without its '
                   f'index because {reason}. Results are still correct once it finishes - '
                   f'try again in a minute.')
    else:
        message = (f'Search timed out after {budget}s.{queued} Try a narrower search, or '
                   f'try again in a moment.')
    return jsonify({'error': message}), 503


@channel_search_bp.route('/api/channels/airings/<int:epg_id>/record-context')
def airing_record_context_api(epg_id):
    """Everything the shared Schedule Recording modal needs for ONE showing.

    The airing grain's Record button opens `templates/_record_modal.html` through
    `static/js/guide.js::openModal`, the same modal the TV Guide opens - and `openModal`
    reads three things the search response deliberately does not carry:

    * **the raw stream URL.** The row payload masks it (`DESIGN-channel-search.md` §9.3), and
      it has to: a stream URL routinely carries the account's credentials in its path, and a
      page of 100 rows re-fetched on every keystroke is the wrong place to put 100 of them.
      The modal posts the URL, so the masked one would schedule a recording of a URL that
      does not exist.
    * **`recording_profile_id`**, which the Edit path selects in the profile picker.
    * **`group_id`**, so a group-backed recording (which carries `group_id` and no
      `channel_id`) is edited as itself rather than silently repointed at one member.

    **`group_id` is never INFERRED, and it is now sometimes ASKED FOR.** Those are different
    things and the distinction is the whole rule. A fresh Record click on a plain airing gets
    no group even when the channel belongs to an in-guide one, because that would silently
    schedule more (a failover-capable recording) than what was clicked (one specific feed) -
    decided against deliberately. But since dev/changelog/811 the airings list can
    draw a row AS a group: "Collapse channel groups" leaves one showing standing for the
    whole group, and that row is labelled with the group's name and its stacked tile. Record
    on THAT row passes `?group=<id>`, and the row the user clicked is what named it. Refusing
    there would schedule a single member off a row that says "group", which is the opposite
    defect.

    The parameter is validated rather than trusted: the group must exist, must not be the
    system group, and the showing's channel must actually be a member of it.

    So this is fetched once per Record CLICK rather than once per row. It is the same
    `_program_dict` the guide grid builds, from the same `recording_match` indexes, so the
    two surfaces cannot disagree about whether a showing is already being recorded.

    Matching tries every group this channel belongs to, not just the channel itself - the
    same order `_recordings_for` (`app/channel_search_rows.py`) uses for the row's own
    has-recording badge, so this endpoint and the row it was opened from never disagree
    about whether a group-backed recording already covers the showing.

    Read-only, and it answers for exactly one EPG id - there is no way to enumerate stream
    URLs through it.
    """
    from ..accounts import effective_filename_template, filename_tag_cleanup, normalize_url
    from ..database import EPGEntry, Recording
    from ..recording_match import build_rec_indexes, candidate_recs, match_recording
    from ..tz_utils import get_display_tz
    # Route-to-route, and deliberately: _program_dict IS the guide's program-cell shape, and
    # the whole point of this endpoint is to hand the modal exactly what the guide hands it.
    # Copying it here would be a second spelling of that shape, which is the drift this area
    # has already paid for four times.
    from .guide import _program_dict

    entry = db.session.get(EPGEntry, epg_id)
    if entry is None:
        return jsonify({'error': 'That showing is no longer in the guide data.'}), 404
    ch = db.session.get(Channel, entry.channel_id)
    if ch is None:
        return jsonify({'error': 'The channel this showing was on no longer exists.'}), 404

    cfg = load_config()
    stream_url = normalize_url(ch.stream_url, ch.account, cfg)
    # Only the recordings that could possibly overlap this one showing, rather than the
    # grid's whole window: this answers for a single click.
    recs = (Recording.query
            .filter(Recording.start_time < entry.stop_time,
                    Recording.stop_time > entry.start_time)
            .all())
    indexes = build_rec_indexes(recs)
    # A channel can be in several groups, so every one of them is tried - a recording only
    # ever carries ONE group_id, so there is no ambiguity in what a match means once found.
    groups = (ChannelGroup.query
              .join(ChannelGroupMember, ChannelGroupMember.group_id == ChannelGroup.id)
              .filter(ChannelGroupMember.channel_id == ch.id).all())
    candidates = []
    for group in groups:
        candidates += candidate_recs(indexes, ch, stream_url, group)
    candidates += candidate_recs(indexes, ch, stream_url)
    rec = match_recording(candidates, entry.start_time, entry.stop_time)

    # The explicit group, when the row that was clicked is a group's row. Validated against
    # the memberships already fetched above, so a hand-typed id cannot point a recording at a
    # group the showing has nothing to do with.
    asked_group = request.args.get('group', type=int)
    chosen_group = None
    if asked_group is not None:
        chosen_group = next((g for g in groups if g.id == asked_group and not g.is_system),
                            None)
        if chosen_group is None:
            return jsonify({'error': 'That showing is not on a member of that channel '
                                     'group.'}), 400

    all_tags = Tag.query.all()
    prog = _program_dict(
        ch, entry, entry.start_time, entry.stop_time,
        stream_url=stream_url,
        template=effective_filename_template(cfg, ch),
        tag_cleanup=filename_tag_cleanup(cfg),
        rec=rec,
        all_tags=all_tags,
        # One showing, so this is not the N+1 case the map exists for - but the parameter is
        # required precisely so a new caller cannot forget it in a loop (dev/changelog/441).
        tags_by_name={t.name: t for t in all_tags},
        tz=get_display_tz(),
        # An existing recording's own group wins - it already settled the question, and
        # editing it must not repoint it. Otherwise the group the CLICKED ROW named, and
        # otherwise nothing. Never a group this endpoint went looking for on its own.
        group_id=(rec.group_id if rec is not None
                  else (chosen_group.id if chosen_group is not None else None)),
    )
    return jsonify({
        'success': True,
        'program': prog,
        # openModal's second argument, which it reads for the channel's default profile.
        'channel': {'id': ch.id, 'default_profile_id': ch.default_profile_id},
        # Named so the modal can say WHICH group it is about to record, rather than the user
        # discovering it on the recording afterwards.
        'group': None if chosen_group is None else {'id': chosen_group.id,
                                                    'name': chosen_group.name},
    })


@channel_search_bp.route('/api/channels/search/catalog')
def channel_search_catalog_api():
    """The registries and vocabularies the search UI renders itself from.

    Fetched once per page load, not per keystroke. Every list here is either a registry
    (constant) or a small aggregate over data that only a sync changes.
    """
    cfg = load_config()
    new_within_days = cfg.get('sync', {}).get('channel_new_within_days', 3)
    _other_new_tip = (
        f"First appeared in the account's synced feed within the last "
        f"{new_within_days} day{'s' if new_within_days != 1 else ''}.")
    accounts = Account.query.order_by(Account.created_at).all()
    tags = Tag.query.order_by(Tag.name).all()
    groups = (ChannelGroup.query
              .filter_by(is_system=False)
              .order_by(db.func.lower(ChannelGroup.name)).all())
    # Distinct, non-empty, sorted - the rail's own vocabulary for the category facet, which
    # has to list a value even when the current search counts zero of it (the three-state
    # control has to let you exclude something you cannot currently see).
    categories = sorted(
        name for (name,) in db.session.query(Channel.category_name).distinct().all() if name)

    return jsonify({
        'success': True,
        'grains': list(IMPLEMENTED_GRAINS),
        'default_grain': GRAIN_CHANNELS,
        'fields': [{'key': f.key, 'label': f.label, 'group': f.group, 'source': f.source,
                    'hint': f.hint}
                   for f in FIELDS],
        # The Search-in pane's headings, in render order. Served rather than spelled in the
        # page, so a group added later cannot end up with fields under no heading.
        'field_groups': [{'name': name, 'help': help_text} for name, help_text in FIELD_GROUPS],
        'default_fields': list(DEFAULT_FIELDS),
        'dimensions': [{'key': d.key, 'label': d.label, 'hidden': d.hidden, 'multi': d.multi,
                        'help': d.help, 'grain': d.grain}
                       for d in DIMENSIONS],
        # `noun` and `hides_when_on` are what let the page render the disclosure line and the
        # toggle counts without re-typing the registry: the noun is the word in "849
        # duplicates", and `hides_when_on` says which way the key reads, since a `show*` key
        # removes rows when ABSENT while `firstonly`/`grpdedup` do when present
        # (dev/changelog/778).
        'standing_options': [{'key': s.key, 'label': s.label, 'default': s.default,
                              'grain': s.grain, 'noun': s.noun,
                              'hides_when_on': s.hides_when_on}
                             for s in STANDING_OPTIONS],
        'default_standing': sorted(DEFAULT_STANDING),
        'sorts': sorted(SORTS),
        'default_sort': DEFAULT_SORT,
        # Everything above describes the channel grain, which is what the page opens on and
        # what every existing caller reads. Everything per-grain is served BESIDE it rather
        # than instead of it, keyed by grain, so a page can render either arrangement from one
        # fetch and a caller that only knows about channels is unaffected.
        'by_grain': {
            grain: {
                # The rail's order, which is not simply the registry's: a dimension that
                # exists only on the grain you switched into leads, because it is the reason
                # you switched (mockup 25, P9).
                'dimensions': [d.key for d in dimensions_for(grain)],
                'standing_options': [s.key for s in standing_options_for(grain)],
                'default_standing': sorted(default_standing_for(grain)),
                'sorts': sorted(SORTS_BY_GRAIN[grain]),
                'default_sort': DEFAULT_SORT_BY_GRAIN[grain],
                # In registry order, not sorted: this list IS what the scope pane ticks
                # and what the page compares its own scope against, and the two grains
                # lead with different fields on purpose (dev/changelog/860).
                'default_fields': list(default_fields_for(grain)),
            }
            for grain in IMPLEMENTED_GRAINS
        },
        # The `when` dimension's fixed values. The two parametrized ones (`next:` and
        # `custom:`) are deliberately absent: there are infinitely many of them, the page
        # builds them from its own controls, and the engine parses whatever arrives.
        'when_values': [{'value': v, 'label': WHEN_STATIC_LABELS[v]}
                        for v in WHEN_STATIC_VALUES],
        'page_size': DEFAULT_PAGE_SIZE,
        'max_page_size': MAX_PAGE_SIZE,
        # Labels carry the configured cut points ("Fair (50-79)"), so they are built from
        # the resolved bands rather than from a constant - move a cut point in Settings and
        # the facet rail says the new number without a second place to edit.
        'health_values': ([{'value': b.key, 'label': b.label} for b in health_bands.resolve_bands(cfg)]
                          + [{'value': HEALTH_UNTESTED, 'label': HEALTH_UNTESTED_LABEL}]),
        # OTHER_NEW alone carries a `tip`: the "how long does a channel show as new"
        # answer is the configured window, which only a runtime cfg read can supply -
        # the other three values are self-explanatory from their label alone.
        'other_values': [
            {'value': v, 'label': OTHER_LABELS[v],
             **({'tip': _other_new_tip} if v == OTHER_NEW else {})}
            for v in OTHER_VALUES
        ],
        'group_any': GROUP_ANY,
        'accounts': [{'id': a.id, 'name': a.name, 'color': a.color} for a in accounts],
        'tags': [{'id': t.id, 'name': t.name, 'color': t.color,
                  'patterns': [p.pattern for p in t.patterns if p.pattern]} for t in tags],
        'groups': [{'id': g.id, 'name': g.name} for g in groups],
        'categories': categories,
        # What the count line says the search is out of ("... of 136,130 channels").
        'total_channels': db.session.query(db.func.count(Channel.id)).scalar() or 0,
    })
