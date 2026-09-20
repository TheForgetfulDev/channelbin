/* ════════════════════════════════════════════════════════════════════
   The channel search page (/channels), rendering app/channel_search.py.

   READ dev/docs/DESIGN-channel-search.md BEFORE CHANGING A PARAMETER NAME.
   The URL parameters and the JSON envelope are an API: other pages link into
   this search, so a rename here is a breaking change to every one of them.

   How the page works, and why:

   * TWO REQUESTS, NOT ONE. Rows are fetched on every keystroke with `facets=`
     (count nothing) because that is 46ms against 667ms with every facet
     attached, measured on the production database. The rail's counts follow on
     a second request that the rows never wait for. A dimension missing from
     `facets` means "not requested" and must render as such - NEVER as zero.
   * THE SERVER OWNS THE CANONICAL URL. Every response carries `query_string`:
     the state as the engine understood it, already minimised. The address bar
     is written from that rather than from a second serializer here, so the two
     cannot drift.
   * ABSENT IS NOT EMPTY, for `standing` and `facets`. Both default to something
     non-empty, so "none of them" cannot be spelled by leaving the parameter
     out - it is one empty value. Dropping it would turn two default-on hiders
     back on, or turn a 46ms fetch into a 667ms one.
   * NOTHING HERE RE-TYPES A REGISTRY. Fields, dimensions, standing options,
     sorts, health bands and the Other labels all come from
     /api/channels/search/catalog, generated from the engine's own tuples.
   ════════════════════════════════════════════════════════════════════ */
(() => {
  'use strict';

  const CFG = window.CHANNEL_SEARCH_CONFIG;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = escHtml;                       // static/js/util.js
  // nf() itself is defined in util.js now (shared with static/js/missing-modal.js).
  // data-tip is read as an attribute, so a newline has to survive attribute
  // escaping; util.js's tooltip renders it with white-space: pre-line.
  const tipAttr = (s) => esc(String(s || '')).replace(/\n/g, '&#10;');

  /* ── The two grains ──────────────────────────────────────────────────
     One state model, one set of filters, one URL contract, TWO RENDERERS.
     Everything except the result table and the sort is shared verbatim, which is
     why this is a toggle on one page rather than a second page
     (DESIGN-channel-search.md 1).

     THE CAVEAT A FUTURE EDITOR GETS WRONG, and it is the reason that section
     exists at all: the standing options and the health / duplicate / in-guide
     facets are CHANNEL-GRAIN CONCEPTS. Under the airing grain they describe the
     channel a showing is ON, never the showing. "Show duplicates" is about two
     channels carrying the same stream URL; it says nothing about two airings.
     The engine honours that structurally by joining Channel into every airing
     query, so nothing here has to remember it - but nothing here may undo it
     either, e.g. by relabelling one of those controls in airing words. */
  const GRAIN_CHANNELS = 'channels';
  const GRAIN_AIRINGS = 'airings';

  /* WHAT the grains are called, which is presentation and therefore local - the
     same kind of table as DIM_ICON and SORT_DIR_WORDS below. The CATALOG owns
     which grains exist and what each one offers; this owns the wording.

     `tab` and `label` are deliberately different, a split that comes from
     mockup 25 round 4: a tab is a place you go, so the strip reads "Guide (EPG)",
     while prose still says "Airings" because "Switched to Guide (EPG)" reads like
     a product announcement and "no Guide (EPG) match this search" is not English.
     One table, two spellings, so neither can be typed at a call site and drift. */
  const GRAIN_UI = {
    [GRAIN_CHANNELS]: {
      label: 'Channels', tab: 'Channels', noun: 'channel', pinLabel: 'Channel', pinSort: 'name',
      what: 'One row per CHANNEL. Answers "which of my channels carry this".\n\n'
          + 'Matching on a guide field here means "this channel is airing something that '
          + 'matches RIGHT NOW" - the program in the Now airing column. Switch to Guide (EPG) '
          + 'to search everything that is on later.',
    },
    [GRAIN_AIRINGS]: {
      label: 'Airings', tab: 'Guide (EPG)', noun: 'airing', pinLabel: 'Program', pinSort: 'title',
      what: 'One row per SHOWING, earliest first. Answers "when is this on".\n\n'
          + 'Matching on a guide field here means "THIS showing matches", so a channel appears '
          + 'once per matching showing and every row carries a time and a Record button.',
    },
  };
  const grainUi = (g) => GRAIN_UI[g || state.grain] || GRAIN_UI[GRAIN_CHANNELS];
  const isAirings = () => state.grain === GRAIN_AIRINGS;

  /* ── The per-grain registries ────────────────────────────────────────
     The catalog serves the channel grain's registries at the top level (every
     caller that predates the airing grain still reads those) and everything
     per-grain under `by_grain`, keyed by grain. These five readers are the ONLY
     way the page asks "what does this grain offer" - so adding a third grain is
     a catalog change, not a hunt through this file.

     They fall back to the channel grain's top-level keys when `by_grain` is
     absent, which is what keeps this file working against an older catalog
     rather than rendering an empty rail. */
  const byGrain = (g) => ((CAT && CAT.by_grain && CAT.by_grain[g || state.grain]) || null);
  const grainKeys = () => (CAT && CAT.grains ? CAT.grains : [GRAIN_CHANNELS]);

  /* Resolved against CAT.dimensions rather than returning bare keys, because the
     order in `by_grain` is NOT the registry's: a dimension that exists only on
     the grain you just switched into LEADS, since it is the reason you switched
     (mockup 25, P9). `when` is therefore first on the airing rail and the channel
     rail is unchanged, having no grain-scoped dimension. */
  function dimsFor(g) {
    if (!CAT) return [];
    const bg = byGrain(g);
    if (!bg || !bg.dimensions) return CAT.dimensions;
    const byKey = Object.fromEntries(CAT.dimensions.map((d) => [d.key, d]));
    return bg.dimensions.map((k) => byKey[k]).filter(Boolean);
  }
  function standingFor(g) {
    if (!CAT) return [];
    const bg = byGrain(g);
    if (!bg || !bg.standing_options) return CAT.standing_options;
    const byKey = Object.fromEntries(CAT.standing_options.map((s) => [s.key, s]));
    return bg.standing_options.map((k) => byKey[k]).filter(Boolean);
  }
  const sortsFor = (g) => {
    const bg = byGrain(g);
    return (bg && bg.sorts) || (CAT ? CAT.sorts : []);
  };
  const defaultSortFor = (g) => {
    const bg = byGrain(g);
    // The literal is the last resort before the catalog lands; it mirrors
    // channel_search.py::DEFAULT_SORT and has to move when that does.
    return (bg && bg.default_sort) || (CAT ? CAT.default_sort : 'name');
  };
  const defaultStandingFor = (g) => {
    const bg = byGrain(g);
    return (bg && bg.default_standing) || (CAT ? CAT.default_standing : []);
  };
  /* The scope a search runs with when the URL names none. Per grain, and the two are
     deliberately disjoint: a channel row is a channel, so it searches the channel's name;
     an airing row is a program, so it searches the three program fields
     (channel_search.py::DEFAULT_FIELDS_BY_GRAIN, dev/changelog/860). */
  const defaultFieldsFor = (g) => {
    const bg = byGrain(g);
    return (bg && bg.default_fields) || (CAT ? CAT.default_fields : []);
  };
  /* Is this option actually REMOVING rows right now? The mirror of
     channel_search.py::standing_applied(), and the one reader of the rule on this side.
     Two families share one set: a `show*` key hides when it is ABSENT, `firstonly` and
     `grpdedup` when they are PRESENT. `state.standing.has(s.key)` is what the toggle's own
     lit state reads - it is NOT this, and using one for the other silently reverses six of
     the eight options. */
  const standingApplied = (s) => state.standing.has(s.key) === s.hides_when_on;

  /* ── The state, which is the URL ─────────────────────────────────────
     Field for field this mirrors SearchState in app/channel_search.py. It is
     the only place the page keeps what is being searched. */
  const state = {
    grain: GRAIN_CHANNELS,
    q: '',
    fields: [],            // registry keys; [] until the catalog lands
    matchAll: true,
    filters: [],           // [{ key, values: [], ex: [] }] in DIMENSIONS order
    standing: new Set(),
    standingExplicit: false,   // did the URL say `standing`? (absent != empty)
    sort: 'name',
    // Did the sort come from the USER (a URL `sort=`, a header click) or from the grain's
    // default? Same absent-vs-chosen distinction standingExplicit draws, and setGrain()
    // needs it for the same reason: six sort keys are valid on both grains, so without it a
    // default the user never picked follows them across the flip and displaces the target
    // grain's own default.
    sortExplicit: false,
    // Same absent-vs-chosen distinction again, for the same reason: the two grains have
    // different default scopes, so a default the user never picked must not follow them
    // across a flip and displace the target grain's own (dev/changelog/860).
    fieldsExplicit: false,
    sortDesc: false,
    page: 1,
    pageSize: 100,
    addToGroup: null,
    // The second action context: the SCHEDULED recording this visit came to replace.
    // Held here rather than read from CFG on demand because dismissing it has to take
    // the parameter out of the URL, and the URL is written from this object.
    replaceRec: null,
    // Set by setGrain() when the sort could not come with you, cleared the moment you
    // pick one yourself. Shown in the count line, because a list that silently
    // reordered itself is exactly the hidden behavior this project forbids.
    sortNote: null,
  };

  /* ── The breakpoint ──────────────────────────────────────────────────
     ONE page serves both widths: the state above, the URL contract, the fetch
     loop, the filters, the standing options, saved searches and the selection
     are identical at 375 and at 1600, and only the DRAWING differs. A second
     template would be a second copy of all of that.

     matchMedia and nothing else, so there is one spelling of 768 in the JS to
     match the one in channel-search.css. (jsdom implements matchMedia but always
     answers `matches: false`, so the test harness stubs it per width - that is
     what makes the breakpoint drivable in a test at all.) */
  const MOBILE_MQ = window.matchMedia('(max-width: 768px)');
  const isMobile = () => MOBILE_MQ.matches;

  /* ── Sheets ──────────────────────────────────────────────────────────
     A sheet IS a modal: style.css's own <=768px block turns .modal-panel into a
     bottom sheet (DESIGN.md 9.6), which is how static/js/guide.js builds every
     one of its sheets. So there is deliberately no second overlay component on
     this page - no bespoke scrim, no second scroll lock, no second Escape
     handler. util.js owns all of that.

     ONE open at a time, and opening one closes the suggestion menu. That last
     part is not decoration: #sugg is absolutely positioned over the results and
     is NOT a .menu, so nothing else closes it, and a sheet opening in front of
     it hands back a page still covered in stale suggestions when it is
     dismissed (found by mockup 22's round-3 browser pass, dev/changelog/394). */
  let sheetEl = null, sheetRedraw = null, sheetKind = null;

  function closeSheet() {
    if (sheetEl && sheetEl.closeModal) sheetEl.closeModal();
    sheetEl = null;
    sheetRedraw = null;
    sheetKind = null;
  }

  /* `kind` is what the chip row tests to decide that tapping an already-open
     sheet's chip closes it. It is read off this one variable rather than off the
     DOM, for the same reason suggOpen is: the sheet's body is rewritten on every
     render and a class read back afterwards is a second source of truth. */
  function openSheet({ kind, title, body, redraw = null, footer = [] }) {
    closeSheet();
    closeSugg();
    closeMenus();
    sheetEl = buildModal({
      title, body, footer, panelClass: 'cs-sheet',
      onClose: () => { sheetEl = null; sheetRedraw = null; sheetKind = null; },
    });
    sheetRedraw = redraw;
    sheetKind = kind;
    return sheetEl;
  }

  /* A sheet that applies live has to redraw when the model moves under it, or a
     tap inside it looks like it did nothing - the page it just changed is behind
     the scrim. Every live sheet therefore carries its own count, which is why
     `redraw` is handed the whole sheet and not only its body: the count lives in
     the title. */
  const sheetBody = () => (sheetEl ? sheetEl.querySelector('.modal-body') : null);
  function setSheetTitle(text) {
    const h = sheetEl && sheetEl.querySelector('.modal-head h2');
    if (h) h.textContent = text;
  }
  function renderOpenSheet() {
    if (sheetEl && sheetRedraw) sheetRedraw();
  }

  let CAT = null;                            // the catalog, once fetched
  const emptyResult = () => ({ rows: [], total: 0, pages: 1, standingHidden: {}, degraded: '' });
  let last = emptyResult();
  /* Is what is on screen an honest answer to the question currently being asked?
     True from boot (nothing has been fetched yet), true again whenever the rows in
     hand stop describing the request that is out. Everything that draws a number off
     the response - the count line, the pager, the heading, the in-box match count -
     reads this and says nothing rather than saying something stale. Set in exactly
     two places, fetchRows() and setGrain(); cleared in exactly one, fetchRows(). */
  let resultsPending = true;
  let loadFailed = '';                       // why the last rows request did not answer
  let facets = {};                           // { dim: { value: count } }
  let facetsCounted = null;                  // null = nothing counted yet
  let facetsPending = false;                 // a fetchFacets() request is in flight
  /* THE THREE WAYS A NUMBER CAN BE MISSING, kept apart because they read differently and
     only one of them is worth waiting through (dev/changelog/676):

       pending   - a request is out. Say so, keep the spinner.
       declined  - the server chose not to compute it (it would cost seconds). It is NOT
                   coming; saying "counting..." would be a lie.
       failed    - the request errored or was shed. Also not coming, and worth a different
                   sentence than a deliberate decline.

     Before this the third case silently reused the first, so a failed counts fetch left
     "counting the total..." on screen forever (BUGS.md 2026-08-16). */
  let countsDeclined = false;
  let countsFailed = '';
  let facetsDeclined = false;
  /* WHY the server declined, in its own words - never re-derived here. There are two
     reasons and they read differently: the index is temporarily unusable (wait), or the
     index cannot answer this search at all (change a control). The page used to hardcode
     the first, which made the second one's copy simply wrong (dev/changelog/681). Empty
     means the server sent no reason, and the fallback wording below stands in. */
  let declinedWhy = '';
  const selected = new Map();                // channel id -> { name, logo_url }

  /* ── One row, two shapes ─────────────────────────────────────────────
     THE SELECTION IS ALWAYS A SET OF CHANNELS, on both grains: all three things you
     can do with a selection (test, add to guide, group) are things you do to a
     channel, and several showings of one program on one channel are ONE selection.

     So everything that asks "which channel is this row about" goes through
     rowChannel(), and everything that asks "which channels are on this page" goes
     through pageChannels() - which DEDUPES, because a page of 100 showings is
     routinely 20 channels and a Select All that reported 100 would be lying.

     This is the one place the two row shapes are reconciled. Every consumer reading
     `r.id` directly was a channel-id read that silently became an AIRING-id read the
     moment the second grain existed - the exact "one flag, one meaning" defect class
     CLAUDE.md names, and the reason this seam is a function rather than a convention. */
  // A GROUP row is not a channel and has none. Everything that asks "which channels are
  // on this page" - Select All, the selection restamp, the suggestion menu's "this exact
  // channel" rows - reads through here, so answering null once is what keeps a group out
  // of every one of them rather than each of them remembering to skip it.
  const rowChannel = (row) => {
    if (!row) return null;
    if (isAirings()) return row.channel;
    return row.kind === 'group' ? null : row;
  };
  function pageChannels() {
    const out = new Map();
    last.rows.forEach((r) => {
      const ch = rowChannel(r);
      if (ch && !out.has(ch.id)) out.set(ch.id, ch);
    });
    return Array.from(out.values());
  }
  const channelOnPage = (id) => pageChannels().find((c) => c.id === id) || null;

  /* ── Text: the client-side mirror of parse_terms() ───────────────────
     duplicated from app/channel_search.py::parse_terms - the well draws a chip
     per term as you type, and asking the server what it parsed would be a round
     trip per keystroke to render something the user just typed. The server
     remains the authority for MATCHING; this only decides what the chips say.
     Kept a pure function of its argument so tests can drive it in node. */
  function parseQuery(raw) {
    const words = [];
    let current = '', quoted = false;
    for (const ch of String(raw || '')) {
      if (ch === '"') quoted = !quoted;
      else if (/\s/.test(ch) && !quoted) { if (current) { words.push(current); current = ''; } }
      else current += ch;
    }
    if (current) words.push(current);
    const inc = [], ex = [];
    for (const word of words) {
      const exclude = word.startsWith('-') && word.length > 1;
      const body = exclude ? word.slice(1) : word;
      if (body) (exclude ? ex : inc).push(body);
    }
    return { inc, ex };
  }
  // hasWildcard/globRegExpBody are util.js globals, loaded before this file.

  /* ── Reading the URL ─────────────────────────────────────────────── */
  function parseState(search) {
    const p = new URLSearchParams(search);
    // FIRST, because the standing and sort defaults below are per grain. An unknown
    // grain falls back rather than throwing: the engine is the authority and would
    // 400 it, but the page still has to render something in the meantime.
    const grain = p.get('grain') || (CAT ? CAT.default_grain : GRAIN_CHANNELS);
    state.grain = grainKeys().includes(grain) ? grain : GRAIN_CHANNELS;
    state.q = (p.get('q') || '').trim();
    const fields = p.getAll('in');
    state.fieldsExplicit = fields.length > 0;
    state.fields = state.fieldsExplicit ? fields : defaultFieldsFor().slice();
    state.matchAll = (p.get('match') || 'all') !== 'any';

    // Filters are rebuilt in DIMENSIONS order, which is what makes a state equal
    // to its own round trip (the engine normalises the same way).
    //
    // EVERY dimension, not this grain's - a filter on a dimension the current grain
    // does not have is PARKED, not dropped, and the engine ignores an out-of-grain
    // filter rather than 400ing it precisely so parking survives a reload
    // (dev/changelog/412). Reading only dimsFor() here would delete the parked chip on
    // every page load, which is the silent-loss defect that decision exists to prevent.
    state.filters = [];
    parked[GRAIN_CHANNELS] = [];
    parked[GRAIN_AIRINGS] = [];
    (CAT ? CAT.dimensions : []).forEach((d) => {
      const values = p.getAll(`f.${d.key}`);
      const ex = p.getAll(`x.${d.key}`);
      if (!values.length && !ex.length) return;
      state.filters.push({ key: d.key, values, ex });
    });

    const standing = p.getAll('standing');
    state.standingExplicit = standing.length > 0;
    state.standing = new Set(
      state.standingExplicit ? standing.filter(Boolean) : defaultStandingFor());

    // to_params() omits `sort` when it equals the grain's default, so an absent parameter
    // genuinely means "I never chose one" rather than "I chose the default".
    state.sortExplicit = p.has('sort');
    const sort = p.get('sort') || defaultSortFor();
    state.sortDesc = sort.startsWith('-');
    state.sort = sort.replace(/^-/, '');
    state.page = Math.max(1, parseInt(p.get('page'), 10) || 1);
    // A URL that names a size always wins. One that names none opens at the Settings
    // default, never by changing what the ENGINE reads into a missing `per_page`: the
    // address bar carries `per_page` whenever it differs from the engine's, so a link copied
    // from this page still opens at the size it was copied at (dev/changelog/1043).
    state.pageSize = Math.min(
      parseInt(p.get('per_page'), 10) || (CAT ? CAT.opening_page_size : 100),
      CAT ? CAT.max_page_size : 500);
    const group = parseInt(p.get('add_to_group'), 10);
    state.addToGroup = Number.isFinite(group) ? group : null;
    // The SERVER decides whether this context is live - it is the side that can see
    // whether the recording is still scheduled. A URL naming one it did not resolve is
    // dropped here, so the address bar cannot keep claiming a replace the page is not
    // offering (the strip would be blank and the Replace buttons would delete nothing).
    const rec = parseInt(p.get('replace_rec'), 10);
    state.replaceRec = (Number.isFinite(rec) && CFG.replaceRec && CFG.replaceRec.id === rec)
      ? rec : null;
  }

  /* ── Writing it back ─────────────────────────────────────────────────
     Everything explicit, defaults included: this is what goes to the server,
     not what goes in the address bar. The short form comes back as
     `query_string` in the response. */
  function toParams({ facets: facetDims, counts, seq } = {}) {
    const p = new URLSearchParams();
    /* Who is asking and which of their requests this is, so the server can drop the work
       when a newer one arrives. Built HERE with everything else the server gets, rather
       than concatenated at the call site, because this function is the one place that
       answers "what does a search request look like". Never reaches the address bar: the
       page puts back the engine's own `query_string`, which is built from the search state
       and knows nothing about these. */
    if (seq !== undefined) {
      p.append('sid', SID);
      p.append('seq', String(seq));
    }
    // Only when it is not the default, so the channel grain's URLs are byte-identical
    // to what they were before this parameter existed and every link already stored
    // still opens the search it named.
    if (state.grain !== GRAIN_CHANNELS) p.append('grain', state.grain);
    if (state.q) p.append('q', state.q);
    state.fields.forEach((k) => p.append('in', k));
    if (!state.matchAll) p.append('match', 'any');
    state.filters.forEach((f) => {
      f.values.forEach((v) => p.append(`f.${f.key}`, v));
      f.ex.forEach((v) => p.append(`x.${f.key}`, v));
    });
    // One empty value when the set is empty, or the server reads the parameter's
    // absence as "use the defaults" and two hiders come back on.
    const standing = Array.from(state.standing).sort();
    if (standing.length) standing.forEach((k) => p.append('standing', k));
    else p.append('standing', '');
    p.append('sort', (state.sortDesc ? '-' : '') + state.sort);
    p.append('page', String(state.page));
    p.append('per_page', String(state.pageSize));
    if (state.addToGroup !== null) p.append('add_to_group', String(state.addToGroup));
    if (state.replaceRec !== null) p.append('replace_rec', String(state.replaceRec));
    if (facetDims) facetDims.forEach((k) => p.append('facets', k));
    else if (facetDims !== undefined) p.append('facets', '');   // count none
    // Absent means "compute it inline" (every caller before this parameter existed, and
    // every one of them still gets that). `false` skips the ~1.6s standing-breakdown query
    // on the airing grain (dev/changelog/598) - fetchCounts() below asks for it separately.
    if (counts === false) p.append('counts', '0');
    return p;
  }

  /* The address bar gets the engine's own minimised spelling, minus `facets`:
     that parameter describes which counts THIS request asked for, and freezing
     the rows-only `facets=` into a shared link would mean the link never counts
     a facet again. */
  function pushUrl(queryString) {
    const p = new URLSearchParams(queryString || '');
    p.delete('facets');
    const qs = p.toString();
    const url = location.pathname + (qs ? `?${qs}` : '');
    if (url !== location.pathname + location.search) history.replaceState(null, '', url);
  }

  /* ── The fetch loop ──────────────────────────────────────────────────
     Sequenced, because a fast empty query can land after a slow broad one and
     a stale response must never paint over a newer one. */
  let rowSeq = 0, facetSeq = 0, applyTimer = null;
  /* AND ABORTED, because the seq guard above only throws away the ANSWER - the request
     it belongs to carries on. During a sync the indexes are stale and every search is an
     unindexed scan of 1.9M rows, so a superseded keystroke leaves minutes of work behind
     it; ten of those on two cores is the 2026-08-01 stampede that killed a sync by
     exhausting the connection pool. Abort SUPPLEMENTS the seq guard and never replaces
     it: an abort can lose the race with a response already in flight, and the sequence
     check is the only thing that guarantees a stale answer never paints.
     What this does NOT do BY ITSELF: stop a scan the server already started. Werkzeug runs
     the handler thread to completion and notices the dead peer only when it writes. It stops
     the ones still queued in the browser behind the 6-connection cap, and frees the slots
     the wanted request is waiting on. dev/changelog/417.

     SO THE ABORT IS ALSO SENT, as data. `sid` (this page load) plus the seq below go on
     every request, and the server treats a strictly newer seq on the same page as
     permission to stop - giving up its place in the scan queue, and aborting the scan
     itself mid-statement if it already had one. Without that, a degraded window spent its
     single scan slot on searches nobody was waiting for: measured live during an index
     rebuild, three searches 0.25s apart ended with the FIRST (already abandoned) one
     getting the only complete answer and the live one 503ing. dev/changelog/678. */
  let rowsAbort = null, facetsAbort = null, countsAbort = null;
  const DEBOUNCE_MS = 250;

  /* One per page load, not per tab or per user: it identifies the sequence counters below,
     and a reload starts a fresh set of them. randomUUID needs a secure context, which this
     app is not guaranteed to be served over, so it falls back rather than throwing. */
  const SID = (crypto.randomUUID ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);

  function scheduleApply(resetPage = true) {
    clearTimeout(applyTimer);
    applyTimer = setTimeout(() => applyNow(resetPage), DEBOUNCE_MS);
  }

  function applyNow(resetPage = true) {
    clearTimeout(applyTimer);
    if (resetPage) state.page = 1;
    // Set BEFORE renderRail() below, not after, so the same paint that shows the stale
    // counts also shows that fresher ones are coming - the single call site of
    // fetchFacets() is this function, so this is the only place that needs to know.
    facetsPending = true;
    // Paint the rail and the bar from local state before either request answers, so a
    // three-state click, a standing toggle or a removed chip is not silent for 700ms. The
    // counts they show are the previous ones until the requests land.
    renderRail();
    renderBar();
    // Fields may just have changed, and which index set the search needs changes with
    // them - so the notice is re-picked here rather than only on the next poll tick.
    renderSearchReadiness();
    fetchRows();
    fetchFacets();
  }

  /* ── Flipping the grain ──────────────────────────────────────────────
     What carries: q, Search in, match mode, every filter chip, the standing
     options and the selection. What cannot: a sort the target grain has never
     heard of, and a filter on a dimension it does not have.

     PARKED FILTERS. `when` exists only on the airing grain, so flipping back to
     Channels would either drop it (round 1's spelling, and the defect that was
     found) or hide it while the URL still carried it - which is a filter nothing
     on screen shows, the exact shape of a silent behavior. Round 4 settled the
     third option: the chip STAYS in the well, dimmed and struck through and
     labelled with the grain it belongs to, and neither direction is toasted
     because a state you can see does not need announcing.

     Held in ONE place while parked, so there is exactly one answer to "where can
     a chip be". Anything that throws the whole filter set away has to throw this
     away with it - see clearAll(). */
  const parked = { [GRAIN_CHANNELS]: [], [GRAIN_AIRINGS]: [] };
  const clearParked = () => {
    parked[GRAIN_CHANNELS] = [];
    parked[GRAIN_AIRINGS] = [];
  };
  // Which dimensions this grain can actually evaluate. A chip on anything else is parked.
  const dimInGrain = (key, g) => dimsFor(g).some((d) => d.key === key);

  function setGrain(g) {
    if (g === state.grain || !GRAIN_UI[g] || !grainKeys().includes(g)) return;
    const from = state.grain;
    /* READ BEFORE THE GRAIN MOVES. Read after, and it is being read in the new
       grain's vocabulary, so the remap below could never fire. */
    const hadSort = state.sort;
    state.grain = g;

    // Park what the grain being LEFT owns and this one cannot express.
    const leaving = state.filters.filter((f) => !dimInGrain(f.key, g));
    if (leaving.length) {
      state.filters = state.filters.filter((f) => leaving.indexOf(f) < 0);
      parked[from] = parked[from].filter((f) => !leaving.some((x) => x.key === f.key))
        .concat(leaving);
    }
    /* Un-park what this grain owns - VALIDATED rather than trusted, because a
       parked chip may have been stored under a registry that has since changed and
       putting back a filter on a dimension that no longer exists would be a chip
       nothing can evaluate. */
    if (parked[g].length) {
      const back = parked[g].filter(
        (f) => dimInGrain(f.key, g) && !findF(f.key));
      parked[g] = [];
      if (back.length) state.filters = state.filters.concat(back);
    }

    /* The sort cannot always come with you: `name` orders channels and `title` /
       `when` order programs, and none of the three means anything on the other
       side. The ENGINE stays strict - an unknown sort is still a 400 - and the page
       simply never generates one, remapping to the target grain's default and
       SAYING SO in the count line until you pick a sort yourself.

       A sort the user never PICKED does not cross either, even when the target grain
       can express it. Six keys are valid on both sides, `category` among them, so
       without this the Channels default followed you into Airings and the airing
       grain's own default (`when`, soonest first) was unreachable by clicking the
       tab - and it is not a cosmetic difference: on the production database the
       default airing page costs 1.67s sorted by category against 0.11s sorted by
       when, because only `when` can walk ix_epg_entries_start_stop and stop at 100
       rows (dev/changelog/692). Unlike the branch above this one discards nothing the
       user asked for, so it is not noted - a default replacing a default is not a
       decision anyone made.

       Which is why the NOTE below is gated on sortExplicit and the remap is not. The
       channel default is `name`, and no airing can be ordered by a channel name, so a
       plain tab click lands in the first branch every time - and a note reading "the
       list was sorted by Channel, which airings cannot be ordered by" would be
       explaining a fallback from a sort the user never picked, on every single flip.
       Both branches remap; only a sort the user actually chose is worth a sentence. */
    const sortWasUsers = state.sortExplicit;
    if (!sortsFor(g).includes(hadSort)) {
      state.sort = defaultSortFor(g);
      state.sortDesc = false;
      state.sortNote = sortWasUsers ? {
        text: `sorted by ${sortLabel(state.sort)}`,
        why: `The list was sorted by "${sortLabel(hadSort, from)}", which ${grainUi(g).label.toLowerCase()}`
           + ' cannot be ordered by, so it fell back to this grain\'s default.\n\n'
           + 'Click any sortable column header to choose your own; this note goes away when you do.',
      } : null;
    } else {
      if (!sortWasUsers) {
        state.sort = defaultSortFor(g);
        state.sortDesc = false;
      }
      state.sortNote = null;
    }

    /* Standing options are per grain too, and the two airings-only ones are ON by
       default. An explicit `standing` in the URL is the user's word and survives;
       otherwise the target grain's defaults apply, or flipping to Airings would
       arrive with `past` and `grpdedup` off and list every showing since the EPG
       began without ever saying it had. */
    /* The SCOPE is per grain too, and the two defaults share no field at all: the channel
       grain searches the channel's name, the airing grain searches the three program
       fields. A scope the user never picked therefore must not cross, or flipping to
       Airings would search program titles for a channel's name and come back empty while
       looking like a working search (dev/changelog/860). One the user DID pick crosses
       unchanged - every field is valid on both grains, since an airing is on a channel. */
    if (!state.fieldsExplicit) state.fields = defaultFieldsFor(g).slice();

    if (!state.standingExplicit) state.standing = new Set(defaultStandingFor(g));
    else {
      // Drop the options the target grain does not offer, so the request cannot carry
      // a key the engine will reject.
      const offered = new Set(standingFor(g).map((s) => s.key));
      state.standing = new Set(Array.from(state.standing).filter((k) => offered.has(k)));
    }

    state.page = 1;
    /* The picker lists whichever grain's registry is active, so it is rebuilt HERE.
       applyColumns() only writes the grid tracks and the header - nothing else
       redraws the popover, and one left listing the other grain's columns is a
       control that silently edits the wrong state. */
    renderColsPop();
    applyColumns();
    /* THE ROWS IN HAND BELONG TO THE GRAIN YOU JUST LEFT, and applyColumns() above has
       already made the header this one's - so leaving them up paints channel rows under
       airing column labels for however long the request takes, which unfiltered is
       ~2 seconds (measured over HTTP on the live database, dev/changelog/420; it was
       6-10s before that fix). They are DISCARDED rather than left to go stale: a flip whose request
       then fails must show a failure, not the other grain's rows quietly coming back.
       renderResults() (not renderGrainToggles(), which it calls) is what puts the
       loading state up, with no timer - this is the wrong-shape case. */
    last = emptyResult();
    resultsPending = true;
    renderResults();
    applyNow();
  }

  /* How long a same-grain request may leave stale rows up before the results area
     gives up on them. Not zero, and that is the whole point - see below. */
  const SLOW_RESULTS_MS = 600;
  let slowTimer = null;

  function fetchRows() {
    const seq = ++rowSeq;
    /* AFTER the bump, never before: the abort rejects the superseded promise, and by
       then rowSeq has already moved past that request's seq so its catch short-circuits
       on the guard rather than reaching the error toast. */
    if (rowsAbort) rowsAbort.abort();
    rowsAbort = new AbortController();
    const ctl = rowsAbort;
    $('#sbox').classList.add('loading');
    loadFailed = '';
    /* THE RESULTS AREA GOES TO A SPINNER WHEN THE ROWS ON SCREEN STOP BEING AN HONEST
       ANSWER, and there are two ways that happens.

       IMMEDIATELY, when they are the WRONG SHAPE: a grain flip, or the first paint with
       no rows at all. setGrain() sets resultsPending itself before it gets here, so this
       function has nothing to decide - it just does not arm the timer over a state that
       is already true. Channel rows under airing column headers is not stale, it is
       wrong, and there is nothing worth looking at for even one frame.

       AFTER SLOW_RESULTS_MS, when they are merely STALE: a keystroke, a page turn, a
       filter, a standing option. Rows 200ms behind read as a responsive page; rows four
       seconds behind read as a page that did not react. The airing grain's row query
       itself is fast (tens of ms, dev/changelog/598 stopped it waiting on the ~1.6s
       count query below) so this timer mostly guards an unindexed-scan fallback now,
       not the common case it was written for - but the delay is still what keeps the
       fast channel grain from blanking on every keystroke, so it stays.

       THE NARROWER RULE IS A ONE-LINE CHANGE, and it is written down as a
       future option (2026-07-31, dev/changelog/415): dropping the
       timer below leaves only the wrong-shape case, so a slow same-grain request keeps
       its stale rows and only the search box spins. DESIGN-channel-search.md §12.2
       carries the reasoning; do not delete one without the other. */
    clearTimeout(slowTimer);
    if (!resultsPending) {
      slowTimer = setTimeout(() => {
        if (seq !== rowSeq) return;          // a newer request owns the area now
        resultsPending = true;
        // The area only: the rail and the bar are not stale when a request is merely
        // slow, and rebuilding the rail under someone mid-keystroke is how focus gets
        // lost (BUGS.md 2026-07-31 09:42 PM).
        renderResultsArea();
      }, SLOW_RESULTS_MS);
    }
    // The airing grain's own standing-breakdown query (total + the "Hide X" counts) is
    // skipped here and fetched separately by fetchCounts() below - it is ~1.6s of an
    // airing request that used to be ~2.1s total, and the row query never depended on
    // its output (dev/changelog/598). The channel grain's breakdown is already cheap
    // (DESIGN-channel-search.md §10), so it stays bundled rather than paying a second
    // round trip for no benefit.
    const skipCounts = isAirings();
    jsonFetch(`${CFG.searchUrl}?${toParams({ facets: null, counts: !skipCounts, seq }).toString()}`,
              { signal: ctl.signal })
      .then((data) => {
        if (seq !== rowSeq) return;            // a newer keystroke already answered
        last = {
          rows: data.rows, total: data.total, pages: data.pages,
          // The two numbers behind `total`, kept apart all the way to the heading: a
          // single blended number over two tables is the one the reader could not
          // explain (DESIGN-group-search-rows.md 5.2).
          channelTotal: data.channel_total, groupTotal: data.group_total || 0,
          standingHidden: data.standing_hidden, degraded: data.degraded || '',
        };
        /* The server declines the counts itself while the index is unusable, whatever this
           request asked for - the rows are what it is protecting. Believe its answer rather
           than re-deriving it from `degraded`: which aggregates it dropped is its call, and
           the page has exactly one speller of that fact. */
        const declined = data.declined || [];
        countsDeclined = declined.includes('counts');
        countsFailed = '';
        declinedWhy = data.declined_reason || '';
        state.page = data.page;
        resultsPending = false;
        pushUrl(data.query_string);
        renderResults();
        /* total is null exactly when this request skipped it - either because the airing
           grain always does, or because the server declined it. Ask separately either way:
           the counts endpoint attempts it on its own budget off the critical path, and on a
           small install (permanently "degraded" because its index was never built) that
           attempt costs microseconds and succeeds. */
        if (data.total === null) fetchCounts(seq);
      })
      .catch((e) => {
        if (seq !== rowSeq) return;
        /* A cancelled request is not a failed one - the guard above already covers every
           abort this page issues, and this is the belt for anything that cancels the
           CURRENT request later (a page teardown, a cancel control). Narrow by name so a
           real failure still reaches the toast. */
        if (e && e.name === 'AbortError') return;
        /* The placeholder has to come down on a failure too, or a spinner sits there
           forever claiming work is still happening. Rows still in hand come back (a
           slow request that failed did not invalidate them); a grain flip discarded
           its rows, so the empty state names the failure instead of claiming nothing
           matched, which would be a lie about the data. */
        loadFailed = e.message || 'Search failed.';
        resultsPending = false;
        renderResults();
        showToast(loadFailed, { type: 'error' });
      })
      .finally(() => {
        if (seq !== rowSeq) return;
        clearTimeout(slowTimer);
        $('#sbox').classList.remove('loading');
      });
  }

  /* The trailing request for whatever fetchRows() just skipped: total, pages, and the
     per-standing-option hidden counts, alone - no rows, no facet rail. `seq` is the ROW
     request's own rowSeq, not a separate counter: these numbers describe that specific
     row response, so a newer row request (which will fire its own fetchCounts, or none
     at all on the channel grain) is what invalidates this one, exactly like the row
     fetch's own guard above.

     Cheap on a warm cache (dev/changelog/598's `_cached_standing_breakdown`) and, on a
     cold one, no worse than what the bundled request used to cost - it just no longer
     blocks the rows the user is already looking at. */
  function fetchCounts(seq) {
    if (countsAbort) countsAbort.abort();
    countsAbort = new AbortController();
    const ctl = countsAbort;
    /* The ROW request's seq, not one of its own - these numbers describe that specific row
       response, so what supersedes it is a newer row request, exactly as the guards below
       already assume. Same lane as the rows on the server for that reason. */
    jsonFetch(`${CFG.countsUrl}?${toParams({ seq }).toString()}`, { signal: ctl.signal })
      .then((data) => {
        if (seq !== rowSeq) return;   // a newer row request has already moved on
        last.total = data.total;
        last.pages = data.pages;
        last.channelTotal = data.channel_total;
        last.groupTotal = data.group_total || 0;
        last.standingHidden = data.standing_hidden;
        // A 200 that declined is not a failure and not a pending state - the total is
        // simply not coming for this search, and renderCount() has to stop promising it.
        countsDeclined = (data.declined || []).includes('counts');
        countsFailed = '';
        /* Only when this answer actually declined something. A counts request that came
           back in full must not wipe the reason a still-declined facet rail is showing -
           the two requests are independent and either can be the one that was too
           expensive. */
        if (countsDeclined) declinedWhy = data.declined_reason || '';
        renderResultsArea();
        renderRail();
        renderBar();
        // The mobile Filters sheet's title carries last.total too - without this it would
        // stay stuck on the pending "matches" wording until some unrelated interaction
        // happened to redraw it.
        renderOpenSheet();
      })
      .catch((e) => {
        if (seq !== rowSeq) return;
        if (e && e.name === 'AbortError') return;
        /* NOT A SILENT SHRUG. The rows are on screen and correct, so this is still not a
           toast - but it is also not "pending". This used to be an empty catch, which left
           the count line reading "Showing these 100 - counting the total..." permanently,
           claiming work was in progress when nothing was and no further request was coming
           (BUGS.md 2026-08-16). Say the total could not be counted instead. */
        countsFailed = e && e.message ? e.message : 'The total could not be counted.';
        countsDeclined = false;
        renderResultsArea();
        renderRail();
        renderBar();
        renderOpenSheet();
      });
  }

  function fetchFacets() {
    if (!CAT) return;
    const dims = dimsFor().filter((d) => !d.hidden).map((d) => d.key);
    const seq = ++facetSeq;
    if (facetsAbort) facetsAbort.abort();
    facetsAbort = new AbortController();
    /* THE RAIL'S OWN ENDPOINT, not the row endpoint with `facets=` on it. That spelling
       ran the full LIMIT-100 row query and threw the rows away, so every keystroke paid the
       page query twice - ~3.4s of pure waste per unindexed airing keystroke on the live
       database, and not free on a healthy one either (dev/changelog/676).

       Still the costlier of the two requests: it runs a scan per dimension, so a superseded
       one is what multiplies hardest during a stale window. */
    jsonFetch(`${CFG.facetsUrl}?${toParams({ facets: dims, seq }).toString()}`,
              { signal: facetsAbort.signal })
      .then((data) => {
        if (seq !== facetSeq) return;
        facets = data.facets || {};
        facetsCounted = data.facets_counted || [];
        facetsDeclined = (data.declined || []).includes('facets');
        if (facetsDeclined) declinedWhy = data.declined_reason || '';
        facetsPending = false;
        renderRail();
      })
      .catch((e) => {
        // A newer request already owns facetsPending's next value - do not clear a flag
        // that no longer describes this one.
        if (seq !== facetSeq) return;
        if (e && e.name === 'AbortError') return;
        facetsPending = false;
        /* The rows are already on screen; the rail's counts stay whatever they were. But
           the spinner has to come down or it claims work is still happening forever, and
           the rail has to say the counts are not being refreshed rather than leave stale
           ones looking current - the same fix the counts line gets above. */
        facetsDeclined = true;
        facetsCounted = [];
        renderRail();
      });
  }

  /* ── Columns ─────────────────────────────────────────────────────────
     Channel is pinned first and is not in the picker, exactly as the recordings
     list pins its name column. This array IS the default order, and the four
     that lead it are the settled default set. `sort` is the
     SORTS key; a column with none renders an unsortable header, which is the
     honest answer for the three the engine cannot order by
     (DESIGN-channel-search.md 7). */
  /* ONE list for both widths. `short` is the card's label track (the desktop's own
     label is too long for a 58px track) and `card` is whether the card draws it by
     default - which is NOT the same set as the table's, because the mockup put the
     stream id on the card's identity line from its first round and that
     arrangement was picked.
     dev/mockups/22-channel-search-mobile.html, dev/changelog/393.

     THE `fr` TRACKS CARRY REAL MINIMUMS AND THE FIXED ONES ARE SMALL, because the row has
     to FIT: every track used to be a hard floor, so the row was over its container's budget
     before any trade between columns was possible and the list had to be scrolled sideways
     at 1440 (dev/changelog/860). A column that wants more room than its floor gets it from
     the leftover space through its `fr` share; what it must never do is refuse to give room
     back. When adding a column, give it the smallest width its content is still readable at
     and let `fr` do the rest - and re-measure at 1440, because jsdom cannot see this. */
  const COLUMNS = [
    { key: 'health',   label: 'Health',     short: 'Health', width: '76px', num: true, sort: 'health', card: true },
    { key: 'airing',   label: 'Now airing', short: 'Now',    width: 'minmax(110px,1.4fr)', cls: 'a-airing', card: true },
    { key: 'groups',   label: 'Groups',     short: 'Group',  width: 'minmax(84px,.7fr)', card: true },
    { key: 'account',  label: 'Account',    short: 'Acct',   width: 'minmax(96px,.7fr)', sort: 'account', card: true },
    { key: 'category', label: 'Category',   short: 'Cat',    width: 'minmax(110px,1.2fr)', off: true, sort: 'category' },
    { key: 'sid',      label: 'Stream id',  short: 'Id',     width: '92px', num: true, off: true, sort: 'sid', cls: 'a-mono', card: true },
    { key: 'tvg',      label: 'EPG id',     short: 'EPG',    width: '140px', off: true, sort: 'tvg', cls: 'a-mono' },
    { key: 'url',      label: 'URL tail',   short: 'URL',    width: '120px', off: true, sort: 'url', cls: 'a-mono' },
    { key: 'tags',     label: 'Tags',       short: 'Tags',   width: '104px', off: true },
  ];
  const COL_BY_KEY = Object.fromEntries(COLUMNS.map((c) => [c.key, c]));

  /* The two sorts a channel GROUP can answer honestly - its own name and its own health
     score - so group rows interleave with channels under them. The other five describe a
     stream or a provider, which a group has none of, and under those the group rows land
     first in name order and the count line says so. Mirrors
     app/channel_search.py::GROUP_SORTS; the server is the authority and this is what draws
     the disclosure, so the two are checked against each other in the page conformance test
     rather than left to drift. */
  const GROUP_SORTS = new Set(['name', 'health']);

  /* ── The AIRING grain's columns ──────────────────────────────────────
     A SECOND registry, not a variant of the first, because only four of these
     mean the same thing in both lists and the two that lead (When, and the
     program itself) do not exist on the other side at all.

     THE PINNED FIRST COLUMN IS THE PROGRAM, mirroring how Channel is pinned on
     the channel grain: the thing the row IS goes first and is not in the picker.
     Its title, its sub-title and the why-chip live in that one cell, exactly as
     the channel cell carries the name, the DUP badge and the KEPT badge.

     THIS IS THE TIGHTER OF THE TWO ARRANGEMENTS and the one to measure first: it
     carries a wide When column, a pinned Program column AND a Record button, so it
     runs out of room before the channel grain does. Its minimums are set the same
     way and for the same reason - see the note on COLUMNS above.

     `sort` is the SORTS key and a column without one renders an unsortable
     header rather than guessing - paging is server-side over the whole result
     set, so a sort that cannot be expressed in SQL cannot be honoured at all
     (DESIGN-channel-search.md 7). Groups and Tags are multi-valued and
     Description is a paragraph. */
  const AIRING_COLUMNS = [
    { key: 'when',     label: 'When',        short: 'When',  width: '168px', sort: 'when', card: true, cls: 'a-whencell' },
    { key: 'health',   label: 'Health',      short: 'Health', width: '76px', num: true, sort: 'health', card: true },
    { key: 'channel',  label: 'Channel',     short: 'Chan',  width: 'minmax(110px,1.3fr)', sort: 'channel', card: true },
    // On by default here and off on the channel grain, which is the grain difference in one
    // line: a row that IS a program is worth describing, a row that is a channel is not.
    { key: 'desc',     label: 'Description', short: 'About', width: 'minmax(120px,1.6fr)', card: true, cls: 'a-desc' },
    { key: 'groups',   label: 'Groups',      short: 'Group', width: 'minmax(84px,.7fr)', card: true },
    // Off by default on this grain only. Which account carries a showing is a property of its
    // channel, and the Channel column already names that channel (dev/changelog/861).
    { key: 'account',  label: 'Account',     short: 'Acct',  width: 'minmax(96px,.7fr)', off: true, sort: 'account' },
    { key: 'category', label: 'Category',    short: 'Cat',   width: 'minmax(110px,1.2fr)', off: true, sort: 'category' },
    { key: 'sid',      label: 'Stream id',   short: 'Id',    width: '92px', num: true, off: true, sort: 'sid', cls: 'a-mono' },
    { key: 'tvg',      label: 'EPG id',      short: 'EPG',   width: '140px', off: true, sort: 'tvg', cls: 'a-mono' },
    { key: 'url',      label: 'URL tail',    short: 'URL',   width: '120px', off: true, sort: 'url', cls: 'a-mono' },
    { key: 'tags',     label: 'Tags',        short: 'Tags',  width: '104px', off: true },
  ];
  const ACOL_BY_KEY = Object.fromEntries(AIRING_COLUMNS.map((c) => [c.key, c]));

  /* One column-state object per grain, built the same way from the registry that
     grain owns, so the array literals above ARE the default order.

     ITS OWN PREF ROW, not a share of the channel grain's. The precedent is already
     in the tree: the phone's card fields are their own /api/user-prefs row rather
     than the desktop's column setup, because "a phone and a desktop legitimately
     want different things". Two grains want different things for exactly the same
     reason, and one shared row would have each overwrite the other every time you
     flipped. */
  const colStates = {
    [GRAIN_CHANNELS]: {
      order: COLUMNS.map((c) => c.key),
      hidden: COLUMNS.filter((c) => c.off).map((c) => c.key),
    },
    [GRAIN_AIRINGS]: {
      order: AIRING_COLUMNS.map((c) => c.key),
      hidden: AIRING_COLUMNS.filter((c) => c.off).map((c) => c.key),
    },
  };
  /* Every renderer reads THESE, never a captured copy, so flipping the grain
     changes what they draw with no second code path. */
  const colReg = () => (isAirings() ? ACOL_BY_KEY : COL_BY_KEY);
  const colList = () => (isAirings() ? AIRING_COLUMNS : COLUMNS);
  const colState = () => colStates[state.grain];
  const visibleCols = () =>
    colState().order.filter((k) => !colState().hidden.includes(k)).map((k) => colReg()[k])
      .filter(Boolean);

  /* Which card LINES are drawn, below the breakpoint. DESIGN.md 9.4 says "no
     Columns control on phones", overridden in mockup 22 round 2
     ("there should still be options to control what columns ... of data are
     shown"). It is a much smaller control than the desktop picker: VISIBILITY
     ONLY, no reordering and no generated stylesheet, because a card line is not a
     table track and cannot slide out from under a header
     (dev/changelog/390 is that defect class).

     Its own pref row, not the column setup's: a phone and a desktop legitimately
     want different things, and one row would make each overwrite the other. */
  /* One Set per grain, for the same reason colStates is: flipping must change what
     the cards draw and what the Fields sheet lists, with no second code path. */
  const cardFieldSets = {
    [GRAIN_CHANNELS]: new Set(COLUMNS.filter((c) => c.card).map((c) => c.key)),
    [GRAIN_AIRINGS]: new Set(AIRING_COLUMNS.filter((c) => c.card).map((c) => c.key)),
  };
  const cardFields = () => cardFieldSets[state.grain];
  const canSort = (key) => !!(key && CAT && sortsFor().includes(key));

  /* The catalog serves `sorts` as bare registry keys - the label of a sortable
     column is the column's own, and `name` is the pinned Channel column that is
     not in the picker. Derived rather than re-typed, so a sort added to the
     engine's SORTS shows up here with its column's wording and never as a key.
     `dir` is presentation (which way "up" reads for that field), the same kind of
     local table as DIM_ICON, so it is not in the catalog. */
  const SORT_DIR_WORDS = {
    name:     ['A to Z', 'Z to A'],
    category: ['A to Z', 'Z to A'],
    health:   ['worst first', 'best first'],
    account:  ['A to Z', 'Z to A'],
    sid:      ['lowest first', 'highest first'],
    tvg:      ['A to Z', 'Z to A'],
    url:      ['A to Z', 'Z to A'],
    // The airing grain's own three. "soonest first" rather than "ascending", because
    // what a time sort does is the one thing here that is worth saying in words.
    title:    ['A to Z', 'Z to A'],
    when:     ['soonest first', 'latest first'],
    channel:  ['A to Z', 'Z to A'],
  };
  /* The label a sort key reads as, in the grain that owns it. Derived from the
     column registries rather than typed out, so a column renamed later cannot leave
     this saying the old word. Both registries are consulted, because setGrain() has
     to name the sort it just LEFT BEHIND - which by definition belongs to the other
     grain. `name` and `title` are the two pinned columns and are in neither picker. */
  function sortLabel(key, grain) {
    if (key === 'name') return 'Channel';
    if (key === 'title') return 'Program';
    const airings = (grain || state.grain) === GRAIN_AIRINGS;
    const own = airings ? ACOL_BY_KEY : COL_BY_KEY;
    const other = airings ? COL_BY_KEY : ACOL_BY_KEY;
    return (own[key] || other[key] || {}).label || key;
  }
  const sortDirWords = (key, desc) => (SORT_DIR_WORDS[key] || ['ascending', 'descending'])[desc ? 1 : 0];

  /* THE TRAP, and it is measured (dev/changelog/390, BUGS.md 2026-07-30 06:14):
     the header and every row are SEPARATE grid containers, so a content-sized
     track (fit-content/max-content/auto) resolves differently in each one and
     every header label slides off its column. fit-content() is therefore only a
     MEASUREMENT here - freezeNameCol() measures the widest pinned cell and rewrites
     the rule with that single px value, so one number is shared by the header and
     every row. Do not put a bare fit-content() back. */
  /* The cap the pinned column may freeze to, PER GRAIN, and the two differ because the
     text does. A program title runs far longer than a channel name, so the airing grain's
     column froze at the full 360px where the channel grain's lands around 190-278px - and
     that difference is the whole of the 66px this table overflowed by at 1440 when it was
     measured in a browser, against none on the channel grain (dev/changelog/414).
     300px is what brings the two arrangements to parity. A program title may ellipsize;
     it has its full text in a tooltip and the phone's card never truncates it at all. */
  const NAME_COL_CAP = { [GRAIN_CHANNELS]: 360, [GRAIN_AIRINGS]: 300 };
  const nameColMeasure = () => `fit-content(${NAME_COL_CAP[state.grain]}px)`;
  /* One frozen width per grain: the two pinned columns hold different text, so one shared
     number would be measured on whichever grain rendered last and applied to the other. */
  const nameColCssByGrain = {
    [GRAIN_CHANNELS]: `fit-content(${NAME_COL_CAP[GRAIN_CHANNELS]}px)`,
    [GRAIN_AIRINGS]: `fit-content(${NAME_COL_CAP[GRAIN_AIRINGS]}px)`,
  };

  /* The trailing ACTION track, per grain. The airing grain still ends in one button
     (Record, whose widest label is "Manage recording"); the channel grain ends in nothing
     at all since its rows stopped carrying "+ Add to Guide" and its group rows stopped
     carrying a guide button (dev/changelog/860). An empty 150px track on every one of
     136,130 rows is exactly the width this page did not have, so it is not emitted rather
     than emitted empty - and `''` here means the row has one fewer track AND one fewer gap. */
  const actionTrack = () => (isAirings() ? ' minmax(132px,.9fr)' : '');

  function applyColumns() {
    const widths = visibleCols().map((c) => c.width).join(' ');
    const nameColCss = nameColCssByGrain[state.grain];
    // Scoped to the desktop breakpoint, exactly as index.html scopes its own
    // generated rule: this tag is late in the document and would otherwise beat
    // the mobile grid-template in the stylesheet.
    $('#col-css').textContent =
      `@media (min-width: 769px) { .arow, .arow-head { grid-template-columns: ` +
      `28px 30px ${nameColCss} ${widths}${actionTrack()}; } }`;
    renderHead();
  }

  // Publishes .cs-bar's used height as --cs-bar-h so .arow-head can park directly
  // under it. Returns without writing anything when there's no bar or it measures
  // 0 (no layout engine, e.g. under jsdom) - the same contract pinnedCellContent()
  // follows below, leaving the fallback in place rather than a meaningless offset.
  function syncBarOffset() {
    const bar = $('.cs-bar');
    if (!bar) return;
    const h = bar.offsetHeight;
    if (!h) return;
    document.documentElement.style.setProperty('--cs-bar-h', `${h}px`);
  }

  // Watches .cs-bar directly instead of re-syncing from a fixed set of "this render
  // might have changed it" call sites. The count line, the well text and the toolbar
  // each wrap independently of window resize - e.g. renderCount()'s async total can
  // arrive and wrap the bar to a second line AFTER the same render pass's own
  // syncBarOffset() call already ran, going stale until the next unrelated render. A
  // ResizeObserver fires on the real box-size change regardless of which render path
  // caused it, so a future content addition to the bar can't reopen this the way the
  // 1440px fix (dev/changelog/557) didn't reach 926px. observe() itself fires once
  // immediately with the current size, so this also covers the initial measurement.
  if (window.ResizeObserver) {
    const barEl = $('.cs-bar');
    if (barEl) new ResizeObserver(syncBarOffset).observe(barEl);
  } else {
    syncBarOffset();
  }

  /* The pinned cell's CONTENT extent - what it would need, not what it currently
     gets. 0 when there is no layout engine to have computed one, which leaves the
     measurement value in place rather than freezing a meaningless number.

     It reads scrollWidth off the cell rather than the used width of the track the
     cell sits in, and the difference is the whole of BUGS.md 2026-08-20 09:12: a
     track asked for `fit-content(360px)` resolved to the 190px `.a-namecell`
     min-width no matter what the cell held, so reading the track back measured the
     floor and froze the column there forever. scrollWidth is the content extent even
     while `.acell`'s `overflow: hidden` is clipping it - measured 310px on a row the
     track reported as 190px - so it is the only one of the two that can see content
     the column is already too narrow for. */
  function pinnedCellContent(el) {
    const cell = el && el.children[2];
    return cell ? cell.scrollWidth || 0 : 0;
  }

  /* The floor a track will not go below, from the width string the registry holds - either
     a bare length or a `minmax()` whose first argument is one. Everything in both registries
     is spelled one of those two ways; anything else is not a track this can budget for and
     is counted as 0 rather than guessed at. */
  const trackMin = (width) => {
    const m = /(-?[\d.]+)px/.exec(String(width || ''));
    return m ? parseFloat(m[1]) : 0;
  };

  /* How much of the list's width is spoken for by everything EXCEPT the pinned column:
     the two fixed lead tracks, every visible column's floor, the action track's floor, the
     gaps between them and `.arow`'s own padding. The numbers come from the registry and
     from CSS custom properties rather than being re-typed here, so moving a gap or a
     padding in channel-search.css cannot leave this budgeting against the old one. */
  const ROW_LEAD_TRACKS = [28, 30];
  function nonNameBudget() {
    const mins = ROW_LEAD_TRACKS
      .concat(visibleCols().map((c) => trackMin(c.width)))
      .concat(actionTrack() ? [trackMin(actionTrack())] : []);
    const style = getComputedStyle(document.documentElement);
    const gap = parseFloat(style.getPropertyValue('--cs-row-gap')) || 6;
    const pad = parseFloat(style.getPropertyValue('--cs-row-pad')) || 6;
    // One more gap than the non-name tracks, because the pinned column sits between them.
    return mins.reduce((a, b) => a + b, 0) + gap * mins.length + pad * 2;
  }

  function freezeNameCol() {
    // Below the breakpoint the generated rule does not apply - and there is no
    // table down there at all - so anything measured belongs to the card grid and
    // must not be frozen into the desktop one. Same helper as everything else that
    // branches on width: two spellings of one breakpoint is how they drift.
    if (isMobile()) return;
    nameColCssByGrain[state.grain] = nameColMeasure();
    applyColumns();
    let w = 0;
    for (const el of [$('#ahead'), ...$$('#ch-list .arow')]) w = Math.max(w, pinnedCellContent(el));
    if (!w) return;
    /* THREE numbers, and the third is what stops this table needing to be scrolled
       sideways (dev/changelog/860). The content extent says what the column WANTS, the
       per-grain cap says what it may ever take, and the room actually left in the list
       says what there IS - and only the third one moves with the window. Freezing on the
       first two alone is what let a page of long names widen the row past its container
       at 1440 while every other track sat on its floor with nothing to give.

       A floor of 190 (`.a-namecell`'s own min-width, which the frozen track cannot beat
       anyway) rather than letting a narrow window drive this to nothing: below that the
       row has stopped being about a channel, and the honest answer is the list's own
       horizontal scrollbar, not a nameless row. */
    const room = ($('#ch-list') || {}).clientWidth || 0;
    const avail = room ? Math.max(190, room - nonNameBudget()) : Infinity;
    nameColCssByGrain[state.grain] =
      `${Math.min(Math.ceil(w), NAME_COL_CAP[state.grain], avail)}px`;
    applyColumns();
  }

  /* Server-side, never localStorage (DESIGN.md 3.11) - the same generic
     /api/user-prefs row the recordings list uses, so the setup follows the user
     across browsers. The route renders it into the first paint (CFG.columnPrefs),
     which is why nothing is fetched here. */
  // Which pref row each grain's setup lives in. The airing keys are separate rows,
  // not a nested object inside the channel one, so a stored channel setup written
  // before the airing grain existed still loads unchanged.
  const columnsPrefKey = () => (isAirings() ? CFG.airingColumnsPrefKey : CFG.columnsPrefKey);
  const cardFieldsPrefKey = () => (isAirings() ? CFG.airingCardFieldsPrefKey : CFG.cardFieldsPrefKey);

  function saveColumns() {
    jsonFetch(CFG.prefUrlBase + columnsPrefKey(), {
      method: 'POST', body: JSON.stringify({ value: colState() }),
    }).catch(() => showToast('Could not save the column setup.', { type: 'error' }));
  }

  function saveCardFields() {
    jsonFetch(CFG.prefUrlBase + cardFieldsPrefKey(), {
      method: 'POST', body: JSON.stringify({ value: Array.from(cardFields()) }),
    }).catch(() => showToast('Could not save which fields the cards show.', { type: 'error' }));
  }

  function renderColsPop() {
    const reg = colReg();
    $('#cols-list').innerHTML = colState().order.map((k) => {
      const c = reg[k];
      if (!c) return '';
      return `<label class="col-item" draggable="true" data-col="${esc(k)}">
        <span class="grip">&#8942;&#8942;</span>
        <input type="checkbox" data-col="${esc(k)}"${colState().hidden.includes(k) ? '' : ' checked'}>
        ${esc(c.label)}</label>`;
    }).join('');
  }

  /* Hiding the column a sort is running on would leave the list ordered by
     something that is no longer on screen, which is the silent state this
     project's founding principle forbids. The PINNED column is the only fallback
     that is always visible, and which one that is depends on the grain - Channel
     on one side, Program on the other. */
  function guardSortAgainstHidden(key) {
    if (state.sort !== key || !canSort(key)) return false;
    const fallback = grainUi().pinSort;
    state.sort = fallback;
    state.sortDesc = false;
    showToast(`Sorting by ${sortLabel(key)} needs that column, so the list is sorted by `
              + `${sortLabel(fallback)} now.`);
    return true;
  }

  function setColumnVisible(key, on) {
    const cs = colState();
    if (on) cs.hidden = cs.hidden.filter((k) => k !== key);
    else if (!cs.hidden.includes(key)) cs.hidden.push(key);
    const resorted = on ? false : guardSortAgainstHidden(key);
    saveColumns();
    applyColumns();
    // A hidden column changes which cells every row draws, so the rows are
    // redrawn from what is already in hand - no request, and no re-sort unless
    // the sort itself just moved.
    // A moved sort re-orders all 136,130 rows, so the page number no longer
    // points at the same slice - that one goes back to page 1.
    if (resorted) applyNow(); else renderResults();
  }

  const sortInd = (key) =>
    state.sort === key ? `<span class="sort-ind">${state.sortDesc ? '&#9660;' : '&#9650;'}</span>` : '';

  function headCell(label, sortKey, extraClass = '') {
    const cls = [extraClass, canSort(sortKey) ? 'sortable' : '',
                 sortKey && state.sort === sortKey ? 'sorted' : ''].filter(Boolean).join(' ');
    const attr = canSort(sortKey) ? ` data-sort="${sortKey}"` : '';
    return `<div class="${cls}"${attr}>${esc(label)}${sortKey ? sortInd(sortKey) : ''}</div>`;
  }

  function renderHead() {
    const ui = grainUi();
    const cells = [
      `<div><input type="checkbox" id="all-select-all" data-tip="Select every ${esc(ui.noun)} on this page."></div>`,
      `<div></div>`,
      headCell(ui.pinLabel, ui.pinSort),
    ];
    visibleCols().forEach((c) => cells.push(headCell(c.label, c.sort, c.num ? 'num' : '')));
    // The header's cell count has to match the row's track count, so the unlabeled spacer
    // over the action column exists only where that column does - see actionTrack().
    if (actionTrack()) cells.push('<div></div>');
    $('#ahead').innerHTML = cells.join('');
  }

  /* ── Rows ────────────────────────────────────────────────────────── */

  // Sentinels, not markup: written as escapes so they stay visible in the source.
  // They survive esc() because they are not HTML, which is what lets a match be
  // marked BEFORE the text is escaped without letting a real < > & reach a tag.
  const MARK_OPEN = '\u0002', MARK_CLOSE = '\u0003';
  function highlight(text) {
    const terms = parseQuery(state.q).inc;
    if (!terms.length) return esc(String(text ?? ''));
    let out = String(text ?? '');
    for (const t of terms) {
      out = out.replace(new RegExp(`(${globRegExpBody(t)})`, 'gi'), (m) => (m ? MARK_OPEN + m + MARK_CLOSE : m));
    }
    // Marked first, escaped second: the sentinels survive esc() because they are
    // not HTML, so no real < > & can be smuggled into a tag.
    return esc(out).split(MARK_OPEN).join('<mark>').split(MARK_CLOSE).join('</mark>');
  }

  // The badge always carries the cluster size: that is what tells a stray pair
  // apart from the six-copy cluster, which is what the user is looking for.
  function dupBadge(row) {
    if (!row.dup) return '';
    const d = row.dup;
    const shown = (d.others || []).slice(0, 4);
    const more = (d.others || []).length > shown.length
      ? `\n  + ${d.others.length - shown.length} more` : '';
    const hidden = d.others_hidden
      ? '\n\nSome of them are hidden right now because "Show duplicates" is off.' : '';
    const tip = `Duplicate stream URL.\n${d.count - 1} other channel` +
      `${d.count === 2 ? '' : 's'} point at the identical URL:\n` +
      shown.map((c) => `  ${c.name}   (${c.category || 'no category'})`).join('\n') + more + hidden +
      '\n\nClick to see just that cluster. A "← Your search" button appears so you can return.';
    return `<span class="badge b-warn" data-act="dup-badge" data-id="${row.id}" data-tip="${tipAttr(tip)}">DUP &times;${d.count}</span>`;
  }

  /* ONE renderer per field, for BOTH surfaces: a table cell wraps it in .acell and
     a card wraps it in a labeled row, and neither re-spells what a field looks
     like. `tip` comes back separately rather than baked into the markup because
     the two wrappers want it in different places - on the ellipsising cell up
     here, on the wrapping value span down there. */
  function fieldValue(key, row) {
    switch (key) {
      case 'category':
        return { html: highlight(row.category || '--'), tip: row.category || 'No category' };
      case 'account': {
        // Opens the account, for the same reason the Groups pill opens its group. The dot
        // rides inside the link so the whole cell is one target rather than a coloured dot
        // beside a separate one.
        const a = row.account || {};
        const dot = `<span class="acct-dot" style="background:${esc(a.color || '#666')}"></span>`;
        if (!a.id) return { html: `${dot} ${esc(a.name || '')}` };
        return { html: `<a href="${CFG.accountUrlBase}${a.id}" class="a-name"
            data-tip="${tipAttr(`${a.name}\n\nOpens this account.`)}">${dot} ${esc(a.name || '')}</a>` };
      }
      case 'sid':
        return { html: row.stream_id ? highlight(String(row.stream_id)) : '--' };
      case 'tvg':
        return { html: row.epg_channel_id ? highlight(row.epg_channel_id) : '--' };
      case 'url':
        // The full (masked) URL is in the tooltip; the cell draws the tail,
        // which is the part that differs between two copies of one channel.
        return {
          html: esc(String(row.stream_url || '').replace(/^https?:\/\/[^/]+\/[^/]+\/[^/]+/, '…')) || '--',
          tip: row.stream_url,
        };
      case 'health':
        // null is "never tested", which is not zero.
        return { html: row.health === null
          ? '<span class="h-mini hb-none" data-tip="Never tested.">--</span>'
          : `<span class="h-mini ${healthBandCss(row.health)}" data-tip="Channel health ${row.health.toFixed(1)}.&#10;Lifetime 0-100 score from this channel's tests and recordings.">&#9733; ${row.health.toFixed(0)}</span>` };
      case 'groups': {
        // The pill OPENS the group it names rather than the channel the row is about -
        // a group name on screen is an address, and the row's own click target is the
        // channel (dev/changelog/860). The tooltip still lists every group.
        const gs = row.groups || [];
        return { html: gs.length
          ? `<a href="${CFG.groupDetailUrlBase}${gs[0].id}" class="badge b-abort"
               data-tip="${tipAttr(`In group: ${gs.map((g) => g.name).join(', ')}`
                 + `\n\nOpens "${gs[0].name}".`)}">&#9939; ${esc(gs[0].name)}${
                 gs.length > 1 ? ` +${gs.length - 1}` : ''}</a>` : '' };
      }
      case 'tags':
        return { html: row.tags.map((t) =>
          `<span class="badge" style="background:${esc(t.color)}22;color:${esc(t.color)}" data-tip="${tipAttr(`Tag "${t.name}"`)}">${esc(t.name)}</span>`).join(' ') };
      case 'airing': {
        const p = row.airing;
        return { html: p
          ? `${highlight(p.title)}${p.sub_title ? ` <span class="ai-k">${highlight(p.sub_title)}</span>` : ''}` : '' };
      }
      default:
        return { html: '' };
    }
  }

  /* THE THIRD GUIDE STATE. `in_guide` means "has a guide row of its own" and nothing
     else since dev/changelog/751, but the "In your guide" filter matches the WIDER
     question - own row OR member of a group that has one - so a row could be filtered in
     while nothing on it said the guide had ever heard of it. This badge is the missing
     half: it names the group whose row carries this channel's listings.

     Rendered whether or not the channel also has its own row: they are two separate
     facts, and a channel can genuinely be in the guide twice over. This is also the badge
     the `showmembers` default turns from a rarity into a common sight - a member now keeps
     a row of its own beside its group's, and this is what explains the pair
     (dev/changelog/860).

     It lives in the NAME CELL, which is a flex row that only truncates `.a-name` itself.
     Every other cell is an `.acell` - `overflow: hidden; white-space: nowrap` over a track
     sized without regard to a group's name - so a long name would be clipped mid-word
     anywhere else. */
  const guideViaBadge = (row) => {
    const via = row.guide_via || [];
    if (!via.length) return '';
    const extra = via.length > 1 ? ` +${via.length - 1}` : '';
    const also = row.in_guide
      ? 'It also has its own guide row.'
      : 'It has no guide row of its own - adding it gives it one as well.';
    const tip = (via.length === 1
      ? `This channel's listings reach your TV Guide through the group "${via[0].name}", which has a row of its own.\n${also}`
      : `This channel's listings reach your TV Guide through ${via.length} groups that have rows:\n`
        + `${via.map((g) => g.name).join('\n')}\n\n${also}`)
      + `\n\nOpens "${via[0].name}".`;
    // A LINK to the group it names, not to the channel this row is about. The badge's
    // whole content is a group's name, so that is where clicking it goes; `data-stop`
    // keeps the row's own open-the-channel click from firing underneath it.
    return `<a href="${CFG.groupDetailUrlBase}${via[0].id}" class="badge b-auto"
        data-tip="${tipAttr(tip)}">In guide via ${esc(via[0].name)}${extra}</a>`;
  };

  /* The ` data-tip="..."` an add-to-guide control carries when the listings are already
     on screen through a group. Empty otherwise, so the control keeps whatever tooltip it
     had - this adds an explanation, it never replaces one. */
  const guideViaTip = (row) => {
    const via = row.guide_via || [];
    if (!via.length) return '';
    return ` data-tip="${tipAttr(
      `Add to Guide.\nThis channel's listings are already in your guide through the group `
      + `"${via[0].name}"${via.length > 1 ? ` and ${via.length - 1} more` : ''}. `
      + 'Adding it gives it a row of its own as well.')}"`;
  };

  /* The lifecycle pair, Not normalized, and the two hidden states.
     THERE IS NO STATUS COLUMN ANY MORE (dev/changelog/860). These five lived in a fixed
     150px track that was blank on nearly every row - three of them can only appear on a
     search that ticked "Show hidden channels" or "Show not-normalized URLs", both off by
     default - so the column cost its width on every row to say something on almost none.
     They are name-cell badges at both widths now, beside DUP, KEPT and "In guide via X",
     which are the same kind of fact and were already there: variable width, zero cost on a
     row that has none. That also means they cannot be switched off, exactly as those three
     cannot - a row that is Missing has to say so wherever you meet it. */
  function statusBadges(row) {
    const bits = [];
    const acct = (row.account || {}).name || '';
    if (row.lifecycle === 'missing') bits.push(`<span class="badge b-warn" data-tip="No longer seen in ${tipAttr(acct)}'s synced feed since ${esc(row.lifecycle_date)}.&#10;Still visible in the TV Guide, groups and scheduled recordings that reference it - only this search hides it.">Missing ${esc(row.lifecycle_date)}</span>`);
    if (row.lifecycle === 'new') bits.push(`<span class="badge b-auto" data-tip="First appeared in ${tipAttr(acct)}'s synced feed on ${esc(row.lifecycle_date)}.">New ${esc(row.lifecycle_date)}</span>`);
    if (row.not_normalized) bits.push('<span class="badge b-warn" data-tip="URL normalization is on for this account, but this stream URL has no user/password/id triplet to rebuild from, so it was left exactly as the provider supplied it.">Not normalized</span>');
    /* A hidden row can only be on screen because "Show hidden channels" is on, so it is
       here on purpose - but it still has to say it is not offered anywhere else, or the
       list looks like every other list. `hidden_deferred` is the other half: something
       wants it hidden and its guide row or group membership is what is still keeping it. */
    if (row.hidden) bits.push('<span class="badge b-abort" data-tip="Hidden.&#10;Not offered in any search, picker or count - this row is here because this search has &quot;Show hidden channels&quot; turned on. Its own page still works, and that is where you un-hide it.">Hidden</span>');
    else if (row.hidden_deferred) bits.push('<span class="badge b-warn" data-tip="Hidden, deferred.&#10;Something says hide this channel, and its TV Guide row or channel-group membership is keeping it visible. It drops out on its own once that is no longer true.">Hidden, deferred</span>');
    return bits;
  }

  function cellHtml(col, row) {
    const { html, tip } = fieldValue(col.key, row);
    const cls = `acell${col.num ? ' num' : ''}${col.cls ? ` ${col.cls}` : ''}`;
    return `<div class="${cls}"${tip ? ` data-tip="${tipAttr(tip)}"` : ''}>${html}</div>`;
  }

  // The why-chip is deliberately absent when the NAME matched (a name match needs
  // no explanation) and when two fields matched one term each - the server decides
  // that; an absent `why` is not a missing value.
  const whyBadge = (row) => (row.why
    ? `<span class="why" data-tip="${tipAttr(row.why.source === 'program'
        ? `Matched on what this channel is airing RIGHT NOW, not on the channel itself.\n${row.why.label}: ${row.why.program || ''}`
        : `Matched on ${row.why.label}, not the name.`)}">${esc(row.why.label)}</span>`
    : '');
  // Says which RUNG of the cascade decided it, not all three.
  const keptBadge = (row) => (row.kept && row.dup
    ? `<span class="kept" data-tip="${tipAttr(`Kept out of the ${row.dup.count} channels sharing this stream URL, because it is ${row.dup.kept_reason}.\nThe rule is: in your guide first, then in a channel group, then best health, then lowest channel id.`)}">KEPT</span>`
    : '');
  const noteBadge = (row) => (row.notes
    ? `<span data-tip="${tipAttr(row.notes)}" style="cursor:help">&#128221;</span>` : '');

  /* Its own guide row, said as a badge rather than as a button. The per-row
     "+ Add to Guide" control is gone from this grain (dev/changelog/860) - the checkbox and
     the selection bar are the one path to the guide, so nothing here acts. What is left is
     the FACT, which still has to be on the row: without it a channel already in the guide
     and one that is not would look identical. */
  const inGuideBadge = (row) => (row.in_guide
    ? '<span class="badge b-done" data-tip="This channel has a TV Guide row of its own.">In Guide</span>'
    : '');

  /* Every variable-width fact about a channel, in one order, for BOTH surfaces. The table
     puts them in the name cell and the card on its badge line, and neither re-spells the
     list - which is what stopped Status drifting between the two while it was a column up
     here and a badge line down there. */
  const channelBadges = (row) => [
    dupBadge(row), keptBadge(row), inGuideBadge(row), guideViaBadge(row),
    ...statusBadges(row), whyBadge(row), noteBadge(row),
  ].filter(Boolean);

  function rowHtml(row) {
    const cells = [
      `<div><input type="checkbox" data-id="${row.id}"${selected.has(row.id) ? ' checked' : ''}></div>`,
      `<div><span class="a-logo" data-act="open-channel" data-id="${row.id}">${esc((row.name || '?')[0])}</span></div>`,
      `<div class="acell a-namecell">
         <a href="${CFG.channelUrlBase}${row.id}" class="a-name" data-tip="${tipAttr(row.name)}">${highlight(row.name)}</a>
         ${channelBadges(row).join('')}
       </div>`,
    ];
    visibleCols().forEach((c) => cells.push(cellHtml(c, row)));

    return `<div class="arow${row.in_guide ? ' in-guide' : ''}${row.lifecycle === 'missing' ? ' is-removed' : ''}" data-id="${row.id}">${cells.join('')}</div>`;
  }

  /* ── One CARD per channel, below the breakpoint ───────────────────────
     THE NAME IS NEVER TRUNCATED (DESIGN.md 9.4): it takes the card's full width
     and wraps. Everything else is a LABELED ROW on two fixed tracks, which is
     the arrangement picked out of four candidates in mockup 22 round 3 -
     the labels line up down the whole list, so no value can be read as a
     different field. Round 1's card put five values on one unlabeled mono line
     that could not be deciphered; that is the defect this shape fixes, so do not
     "tidy" the labels away.

     None of the badges is a card field and none can be switched off - the same
     list, in the same order, as the table's name cell. */
  function cardBadges(row) {
    const bits = channelBadges(row);
    return bits.length ? `<div class="cc-badges">${bits.join('')}</div>` : '';
  }

  function cardHtml(row) {
    const rows = COLUMNS.filter((c) => cardFields().has(c.key))
      .map((c) => ({ c, html: fieldValue(c.key, row).html }))
      .filter((p) => p.html)
      .map((p) => `<span class="cr-l">${esc(p.c.short)}</span><span class="cr-v">${p.html}</span>`)
      .join('');
    return `<div class="ccard${row.in_guide ? ' in-guide' : ''}${
        row.lifecycle === 'missing' ? ' is-removed' : ''}" data-id="${row.id}">
      <div><input type="checkbox" data-id="${row.id}"${selected.has(row.id) ? ' checked' : ''}
        aria-label="Select ${esc(row.name)}"></div>
      <div class="cc-logo" data-act="open-channel" data-id="${row.id}">${esc((row.name || '?')[0])}</div>
      <div class="cc-body">
        <div class="cc-name" data-act="open-channel" data-id="${row.id}">${highlight(row.name)}</div>
        ${cardBadges(row)}
        ${rows ? `<div class="cc-rows">${rows}</div>` : ''}
      </div>
      <button class="cc-kebab" data-rowkebab="${row.id}" type="button"
        aria-label="More for ${esc(row.name)}">&#8943;</button>
    </div>`;
  }

  /* ══ The GROUP row and card ═══════════════════════════════════════════
     A channel group is a row kind of its own, inline in the channel list and folded
     into by its members (DESIGN-group-search-rows.md §5.1). Three cues say so and all
     three are existing vocabulary rather than new design: a 2px inset left edge (the
     idiom .arow-air.is-onnow already uses, in the accent rather than --live because
     this is an identity and not a live state), a stacked logo tile inside the same
     26px/30px footprint, and a `.badge b-auto` pill.

     NO ACTIONS AT ALL, and no selection checkbox (§5.2). Every button on the selection bar
     (Test, Add to guide, Group, Hide) is a channel action, which is why a group has never
     been selectable here; and its own "+ Add to Guide" went with the channel row's
     (dev/changelog/860). Putting a group in the guide is not a one-click fact the way it is
     for a channel - the row it gets is filled by whichever member the group's participation
     switches and format lock leave eligible, so a group that is not set up yet gets a guide
     row that produces nothing. That decision belongs on the group's own page, which this
     row's name links to. What is left here is identity and values: `--` plus a REASON for
     stream id, EPG id, URL and category, from the server with the row (`no_value`), because
     they are facts about the data model. */
  const groupPill = (g) => '<span class="badge b-auto" data-tip="' + tipAttr(
    'A channel group, not a channel. Recording it records the group, and it fails over to '
    + 'another member if the one it starts on drops.') + '">Group</span>';

  const groupFormatBadge = (g) => (g.format_label
    ? `<span class="badge b-abort" data-tip="${tipAttr(
        `Format locked to ${g.format_label}. Members whose measured format does not match are `
        + 'not picked for recordings; they are left exactly as you set them.')
      }">${esc(g.format_label)}</span>`
    : '');

  const groupGuideBadge = (g) => (g.in_guide
    ? '<span class="badge b-done" data-tip="This group has a TV Guide row of its own, filled by its members.">In Guide</span>'
    : '');

  const groupCheckBadge = (g) => (g.check_only
    ? '<span class="badge b-abort" data-tip="Health checks only. This group is not a recording source, so it offers no Record action.">Checks only</span>'
    : '');

  /* The quieter second line of the group's identity - the same kind of thing .a-psub is
     to a program title, so it is the same treatment rather than a second one. */
  function groupVia(g) {
    const counts = `${nf(g.member_count)} channel${g.member_count === 1 ? '' : 's'}`
      + ` &middot; ${nf(g.recording_member_count)} recording-enabled`;
    if (!g.serving) {
      return `<span class="gvia" data-tip="${tipAttr(
        'No member of this group is recording-enabled, so nothing would serve a recording '
        + 'from it. Turn Recording on for a member on the group\'s own page.')
        }"><span class="gvia-k">via</span> --  &middot; ${counts}</span>`;
    }
    return `<span class="gvia" data-tip="${tipAttr(
      `${g.serving.name} is the member this group would record from right now - the best-ranked `
      + 'recording-enabled member its format lock allows. It can change before and during a '
      + 'recording.')}"><span class="gvia-k">via</span> ${esc(g.serving.name)} &middot; ${counts}</span>`;
  }

  /* A GROUP's answer to a channel column. The ones it cannot answer say `--` and carry
     the server's reason, rather than showing a member's value as if it were the group's -
     a group with one member's stream id in its Stream id cell would read as a channel. */
  function groupFieldValue(key, g) {
    const reason = (g.no_value || {})[key];
    if (reason) return { html: '<span class="text-faint">--</span>', tip: reason };
    switch (key) {
      case 'health':
        return { html: g.health === null
          ? '<span class="h-mini hb-none" data-tip="This group has never been scored.">--</span>'
          : `<span class="h-mini ${healthBandCss(g.health)}" data-tip="Group health ${g.health.toFixed(1)}.&#10;Lifetime 0-100 score across every recording made from this group, all members combined.">&#9733; ${g.health.toFixed(0)}</span>` };
      case 'airing': {
        const p = g.airing;
        if (!p) return { html: '' };
        return {
          html: `${highlight(p.title)}${p.sub_title ? ` <span class="ai-k">${highlight(p.sub_title)}</span>` : ''}`,
          tip: g.serving ? `Read from ${g.serving.name}, the member this group would record from.` : '',
        };
      }
      default:
        return { html: '' };
    }
  }

  function groupCellHtml(col, g) {
    const { html, tip } = groupFieldValue(col.key, g);
    const cls = `acell${col.num ? ' num' : ''}${col.cls ? ` ${col.cls}` : ''}`;
    return `<div class="${cls}"${tip ? ` data-tip="${tipAttr(tip)}"` : ''}>${html}</div>`;
  }

  function groupRowHtml(g) {
    const cells = [
      `<div class="a-nosel" data-tip="${tipAttr(
        'A group is not selectable here. Every action on the selection bar - Test, Add to '
        + 'guide, Group, Hide - is a channel action, and a group answers none of them the '
        + "same way.\n\nIts own actions are on this row and on the group's page.")}">&middot;</div>`,
      `<div><span class="a-logo is-grouplogo" data-act="open-group" data-id="${g.id}"
         data-tip="${tipAttr(`${g.name}\nA channel group of ${g.member_count} members.`)}"
         >${esc((g.name || '?')[0])}</span></div>`,
      `<div class="acell a-namecell is-groupcell">
         <a href="${CFG.groupDetailUrlBase}${g.id}" class="a-name"
            data-tip="${tipAttr(`${g.name}\n\nOpens the group.`)}">${highlight(g.name)}</a>
         ${groupPill(g)}${groupFormatBadge(g)}${groupGuideBadge(g)}${groupCheckBadge(g)}
         ${groupVia(g)}
       </div>`,
    ];
    visibleCols().forEach((c) => cells.push(groupCellHtml(c, g)));
    return `<div class="arow is-group" data-group="${g.id}">${cells.join('')}</div>`;
  }

  /* The same subtraction on a card: no checkbox (its track holds the dot that says why),
     no per-card button - the shipped card carries none, because the checkbox plus the
     bottom bar is the path - and the group's own actions behind the kebab. The member
     counts become a labeled card row, which is what a card does with a value. */
  function groupCardHtml(g) {
    const rows = COLUMNS.filter((c) => cardFields().has(c.key))
      .map((c) => ({ c, html: groupFieldValue(c.key, g).html }))
      .filter((p) => p.html)
      .map((p) => `<span class="cr-l">${esc(p.c.short)}</span><span class="cr-v">${p.html}</span>`)
      .join('');
    const memberRow = '<span class="cr-l">Members</span><span class="cr-v">'
      + `${nf(g.member_count)} channel${g.member_count === 1 ? '' : 's'} &middot; `
      + `${nf(g.recording_member_count)} recording-enabled</span>`;
    const badges = [groupPill(g), groupFormatBadge(g), groupGuideBadge(g), groupCheckBadge(g)]
      .filter(Boolean).join('');
    return `<div class="ccard is-group" data-group="${g.id}">
      <div class="a-nosel" data-tip="${tipAttr(
        'A group is not selectable here. Every action on the selection bar - Test, Add to '
        + 'guide, Group, Hide - is a channel action, and a group answers none of them the '
        + "same way.\n\nIts own actions are on the group's page.")}">&middot;</div>
      <div class="cc-logo is-grouplogo" data-act="open-group" data-id="${g.id}">${esc((g.name || '?')[0])}</div>
      <div class="cc-body">
        <div class="cc-name" data-act="open-group" data-id="${g.id}">${highlight(g.name)}</div>
        <div class="cc-badges">${badges}</div>
        <div class="cc-rows">${memberRow}${rows}</div>
      </div>
      <button class="cc-kebab" data-groupkebab="${g.id}" type="button"
        aria-label="More for ${esc(g.name)}">&#8943;</button>
    </div>`;
  }

  /* ══ The AIRING grain's row and card ══════════════════════════════════
     Same skeleton as the channel row and card above, so the two lists read as one
     component: checkbox, a 30px marker cell, the pinned identity cell, then the
     visible columns, then the actions. Only the contents differ.

     TIME IS ALWAYS DRAWN IN THE DISPLAY TIMEZONE and never in a hardcoded one
     (CLAUDE.md, Timezone Rules). The payload sends naive-UTC ISO strings, which
     JavaScript parses as LOCAL unless the Z is put back - that is what utcIsoToDate()
     is for. All four helpers below are util.js's, shared with the guide; util.js
     caches the underlying Intl formatters, which matters here because a result page
     formats a time and a day label for every one of its rows. */
  const utcDate = utcIsoToDate;
  const dayKey = tzDayKey;
  const dayLabel = tzDayLabel;

  /* How long until it starts, in the app's own words. Only rendered while it is
     short enough to be useful - "in 3 days" is what the day label already said. */
  function startsIn(row) {
    if (row.on_now) return 'on now';
    if (row.ended) return '';
    const start = utcDate(row.start_time);
    if (!start) return '';
    const mins = Math.round((start - new Date()) / 60000);
    if (mins < 0) return '';
    if (mins < 1) return 'starting';
    if (mins < 60) return `in ${mins} min`;
    if (mins < 24 * 60) {
      const h = Math.floor(mins / 60), m = mins % 60;
      return `in ${h}h${m ? ` ${m}m` : ''}`;
    }
    return '';
  }

  const timeRange = (row) => {
    const a = utcDate(row.start_time), b = utcDate(row.stop_time);
    return a && b ? `${fmtTimeTz(a)} to ${fmtTimeTz(b)}` : '';
  };

  /* ── The Record action: five states, all PER SHOWING ──────────────────
     Never per channel. The same channel can have one airing recording, one
     scheduled and one neither, so a button reading a channel-level flag would say
     the same wrong thing on all three rows - which is why `record_state` is on the
     showing in the payload (DESIGN-channel-search.md 9.4).

     The fifth state is the one the channel grain has never had: a showing that has
     already ENDED. It cannot offer to record, and the honest answer is a disabled
     control that says why - not a missing button, which reads as a rendering bug,
     and not an enabled one that fails on click.

     The four live labels are the ones the Extended Search modal already uses
     (static/js/guide.js::renderExtResults), carried over rather than reinvented,
     because they are what this app has always said. */
  const REC_ACTIONS = {
    recording: { act: 'rec-manage', cls: 'btn btn-sm btn-primary', label: 'Manage recording',
      tip: 'Recording right now. Opens the recording so you can stop it, extend it or watch it.' },
    scheduled: { act: 'rec-edit', cls: 'btn btn-sm btn-primary', label: 'Edit recording',
      tip: 'Already scheduled. Opens the scheduled recording so you can change its padding, profile or channel.' },
    recorded: { act: 'rec-again', cls: 'btn btn-sm', label: 'Re-record',
      tip: 'This showing was recorded. Recording it again makes a second file - it does not replace the first.' },
    none: { act: 'rec-new', cls: 'btn btn-sm btn-primary', label: 'Record',
      tip: 'Schedule a recording of this showing on this channel.\nOpens the Schedule Recording modal with '
         + 'the channel, the times and the title already filled in.' },
  };

  /* Under the replace context, an unrecorded showing's action is Replace instead of
     Record. Only that one state changes: a showing that already has its own recording
     is what Edit/Manage/Re-record is for, and the old Extended Search modal gated its
     own Replace button exactly the same way. */
  const REC_ACTION_REPLACE = {
    act: 'rec-new', cls: 'btn btn-sm btn-warning', label: 'Replace',
    tip: 'Schedule this showing AND delete the recording you came here to replace.\nOne action, '
       + 'not two - the modal opens with the channel, the times and the title filled in, and '
       + 'saving it removes the original.',
  };

  const recAction = (row) => (
    state.replaceRec !== null && (row.record_state === 'none' || !row.record_state)
      ? REC_ACTION_REPLACE
      : (REC_ACTIONS[row.record_state] || REC_ACTIONS.none));

  function recordBtn(row) {
    if (row.record_state === 'past') {
      const stop = utcDate(row.stop_time);
      return `<button class="btn btn-sm" disabled data-tip="${tipAttr(
        `This showing ended ${dayLabel(stop)} at ${stop ? fmtTimeTz(stop) : ''}, so there is nothing `
        + 'left to record.\n\nIt is in this list because the standing option "Show airings that have '
        + 'ended" is on.')}">Ended</button>`;
    }
    const g = row.group;
    /* A health-check-only group manages no recording source, so it offers no Record - a
       dead button reading "Record" on a row that says "Checks only" is worse than saying
       why (DESIGN-channel-groups-model.md DECIDED 2). */
    if (g && g.check_only) {
      return `<button class="btn btn-sm" disabled data-tip="${tipAttr(
        `"${g.name}" is a health-check group. It is monitored, not recorded from, so there `
        + 'is nothing to schedule here. Its members can still be recorded from their own '
        + 'rows - turn off "Collapse channel groups" to see them.')}">Checks only</button>`;
    }
    const a = recAction(row);
    /* `data-group` is what turns Record on a GROUP's row into a group recording. The row
       the user clicked is what names it - the endpoint still never infers a group on its
       own, and validates that this showing's channel really is a member (dev/changelog/811). */
    return `<button class="${a.cls}" data-act="${a.act}" data-air="${row.id}" data-id="${row.channel.id}"${
      g ? ` data-group="${g.id}"` : ''}
      data-tip="${tipAttr(a.tip + (g ? `\n\nThis records the GROUP "${g.name}", which fails over `
        + 'to another member if the one it starts on drops.' : ''))}">${esc(a.label)}</button>`;
  }

  /* The card shows the STATE even though the ACTION is kebab-only at phone width
     ("P4 - kebab only", mockup 26 round 8): a state readable only behind a tap is
     not a state the list shows. */
  const REC_BADGE = {
    recording: { cls: 'b-live', label: 'Recording', tip: 'This showing is being recorded right now.' },
    scheduled: { cls: 'b-auto', label: 'Scheduled', tip: 'A recording is already scheduled for this showing.' },
    recorded: { cls: 'b-done', label: 'Recorded', tip: 'This showing has been recorded.' },
  };
  const recBadge = (row) => {
    const b = REC_BADGE[row.record_state];
    return b ? `<span class="badge ${b.cls}" data-tip="${tipAttr(b.tip)}">${esc(b.label)}</span>` : '';
  };

  /* ONE renderer per airing field, for BOTH surfaces - the twin of fieldValue().
     Kept SEPARATE rather than folded into that one with a grain test inside every
     case: the two registries share only four keys that mean the same thing, and a
     shared switch would have to test the grain in each of the other eight, which is
     where one of them eventually gets missed. */
  function airingFieldValue(key, row) {
    const ch = row.channel;
    switch (key) {
      case 'when': {
        const start = utcDate(row.start_time), stop = utcDate(row.stop_time);
        const rel = startsIn(row);
        const mins = start && stop ? Math.round((stop - start) / 60000) : 0;
        return {
          html: `<span class="a-when${row.on_now ? ' on-now' : ''}${row.ended ? ' is-past' : ''}"
            ><span class="aw-day">${esc(dayLabel(start))}</span><span class="aw-time">${esc(timeRange(row))}</span>${
            rel ? `<span class="aw-rel${row.on_now ? ' now' : ''}">${esc(rel)}</span>` : ''}</span>`,
          tip: `${dayLabel(start)}, ${timeRange(row)}\n${mins} minutes`
             + `\n\nShown in ${displayTz()}, the display timezone. Stored naive UTC.`,
        };
      }
      /* Links to the channel's own detail page, which closes a standing backlog ask
         ("Deep search results: link channels to detail"). The stream-id tail that item
         also wanted is the Stream id column, one tick away in the picker, rather than a
         second thing crammed into this cell.
         NO ACCOUNT DOT here (mockup 25 round 8): there is an Account column
         already, and the channel grain's own name cell has never carried one either. */
      case 'channel': {
        /* A row that STANDS FOR a group is named after the group, not after whichever
           member happened to survive the collapse - that relabelling is the whole reason
           "Collapse channel groups" could name the group all along and did not
           (DESIGN-group-search-rows.md §5.1). The member it would actually record from is
           still said, in the quieter second line, because "record the group" has to say
           what it will open. */
        const g = row.group;
        if (g) {
          return {
            html: `<a href="${CFG.groupDetailUrlBase}${g.id}" class="a-name"
                data-tip="${tipAttr(`${g.name}\nA channel group of ${g.member_count} members.`
                  + '\n\nOpens the group.')}">${highlight(g.name)}</a>
              ${groupPill(g)}${groupFormatBadge(g)}${groupGuideBadge(g)}
              <span class="gvia" data-tip="${tipAttr(
                `${ch.name} is the member this group would record this showing from - the `
                + 'best-ranked recording-enabled member its format lock allows.')
              }"><span class="gvia-k">via</span> ${esc(ch.name)}</span>`,
            groupcell: true,
          };
        }
        /* The lifecycle badges join the ones already here rather than getting a column of
           their own - the Status column is gone from both grains (dev/changelog/860), and
           this is where this row already says things about its channel. */
        return { html: `<a href="${CFG.channelUrlBase}${ch.id}" class="a-name"
            data-tip="${tipAttr(`${ch.name}\n${ch.category || 'no category'}\n\nOpens this channel.`)}"
            >${highlight(ch.name)}</a>${ch.in_guide
            ? ' <span class="badge b-done" data-tip="This channel has a TV Guide row of its own.">Guide</span>' : ''
            } ${guideViaBadge(ch)} ${dupBadge(ch)} ${statusBadges(ch).join(' ')}` };
      }
      case 'desc':
        return { html: row.description ? highlight(row.description) : '',
                 tip: row.description || 'No description' };
      // Everything else is a CHANNEL value and means exactly what it means on the
      // other grain, so it is answered by that grain's own renderer rather than
      // written twice. THIS is the seam DESIGN-channel-search.md 1 describes: these
      // describe the channel a showing is on, never the showing.
      default:
        return fieldValue(key, ch);
    }
  }

  function airingCellHtml(col, row) {
    const { html, tip, groupcell } = airingFieldValue(col.key, row);
    const cls = `acell${col.num ? ' num' : ''}${col.cls ? ` ${col.cls}` : ''}${
      groupcell ? ' a-namecell is-groupcell' : ''}`;
    return `<div class="${cls}"${tip ? ` data-tip="${tipAttr(tip)}"` : ''}>${html}</div>`;
  }

  /* THE WHY CHIP IS QUIETER HERE, on purpose. On the channel grain it exists for a
     surprise - "this row matched something it is AIRING" - because the program is
     not on screen. On this grain the program IS the row, so a title or sub-title hit
     needs no explanation and only a CHANNEL-side hit is worth a chip. The server
     already applies that rule (9.4), so an absent `why` is the answer, not a gap. */
  const airingWhy = (row) => (row.why
    ? `<span class="why" data-tip="${tipAttr(
        `Matched on the CHANNEL's ${row.why.label}, not on anything in this program.`)}">${esc(row.why.label)}</span>`
    : '');

  function airingRowHtml(row) {
    const ch = row.channel;
    const cells = [
      `<div><input type="checkbox" data-air="${row.id}" data-id="${ch.id}"${selected.has(ch.id) ? ' checked' : ''}
         data-tip="Selecting a showing selects its CHANNEL - several showings on one channel are one selection."></div>`,
      // The 30px track is the channel LOGO on this grain too, not a state glyph
      // (mockup 25 round 8). One writer for both grains would be nice, but the channel
      // row's copy carries a tooltip about the channel and this one has to say which
      // channel a SHOWING is on - the same markup answering two different questions.
      `<div><span class="a-logo${row.group ? ' is-grouplogo' : ''}"
         data-act="${row.group ? 'open-group' : 'open-channel'}"
         data-id="${row.group ? row.group.id : ch.id}"
         data-tip="${tipAttr(row.group
           ? `${row.group.name}\nA channel group of ${row.group.member_count} members.`
           : `${ch.name}\nThe channel this showing is on.`)}"
         >${esc(((row.group ? row.group.name : ch.name) || '?')[0])}</span></div>`,
      /* A real link, like the channel grain's name: it is where a person clicks, and only
         an <a> gets Ctrl-click, middle-click and "Open in new tab" from the browser for free
         (dev/changelog/996). It opens what the row opens. */
      `<div class="acell a-namecell a-progcell">
         <a href="${row.group ? `${CFG.groupDetailUrlBase}${row.group.id}` : `${CFG.channelUrlBase}${ch.id}`}"
           class="a-ptitle" data-tip="${tipAttr(row.title + (row.sub_title ? `\n${row.sub_title}` : '')
             + (row.group ? '\n\nOpens the group.' : '\n\nOpens this channel.'))}"
           >${highlight(row.title)}</a>
         ${row.sub_title ? `<span class="a-psub">${highlight(row.sub_title)}</span>` : ''}
         ${airingWhy(row)}
       </div>`,
    ];
    visibleCols().forEach((c) => cells.push(airingCellHtml(c, row)));
    // RECORD ALONE. The "+ Guide" button that sat beside it was the one control on this row
    // that did not act on the showing the row is about - its own tooltip had to say it was
    // "not needed to record this showing" - and it made the widest arrangement in the app
    // two buttons wide (dev/changelog/860). Putting the channel in the guide is a channel
    // action and lives on the channel grain's selection bar.
    cells.push(`<div class="a-act">${recordBtn(row)}</div>`);

    return `<div class="arow arow-air${row.on_now ? ' is-onnow' : ''}${row.ended ? ' is-past' : ''}${
      ch.lifecycle === 'missing' ? ' is-removed' : ''}${row.group ? ' is-group' : ''}" data-air="${row.id}" data-id="${ch.id}">${cells.join('')}</div>`;
  }

  function airingCardBadges(row) {
    const ch = row.channel;
    // FIRST, because it is the only badge about this SHOWING rather than about its
    // channel, and because under "kebab only" it is the sole thing on the card
    // saying a recording exists at all.
    // The Group pill leads when this row stands for one: it is what the row IS, and the
    // rest of the badges describe the member underneath it.
    const bits = [
      row.group ? groupPill(row.group) : '', row.group ? groupFormatBadge(row.group) : '',
      recBadge(row), dupBadge(ch), inGuideBadge(ch), guideViaBadge(ch),
      ...statusBadges(ch), airingWhy(row), noteBadge(ch),
    ].filter(Boolean);
    return bits.length ? `<div class="cc-badges">${bits.join('')}</div>` : '';
  }

  function airingCardHtml(row) {
    const ch = row.channel;
    const rows = AIRING_COLUMNS.filter((c) => cardFields().has(c.key))
      .map((c) => ({ c, html: airingFieldValue(c.key, row).html }))
      .filter((p) => p.html)
      .map((p) => `<span class="cr-l">${esc(p.c.short)}</span><span class="cr-v">${p.html}</span>`)
      .join('');
    /* The title and the sub-title are ONE identity block, not a field and a field:
       the sub-title is what tells two showings of "Premier League" apart, so
       switching it off separately would leave a list of identical rows. */
    return `<div class="ccard acard${row.on_now ? ' is-onnow' : ''}${row.ended ? ' is-past' : ''}${
        ch.lifecycle === 'missing' ? ' is-removed' : ''}${row.group ? ' is-group' : ''}" data-air="${row.id}" data-id="${ch.id}">
      <div><input type="checkbox" data-air="${row.id}" data-id="${ch.id}"${selected.has(ch.id) ? ' checked' : ''}
        data-tip="Selecting a showing selects its CHANNEL - several showings on one channel are one selection."
        aria-label="Select ${esc(ch.name)}"></div>
      <div class="cc-logo${row.group ? ' is-grouplogo' : ''}"
        data-act="${row.group ? 'open-group' : 'open-channel'}"
        data-id="${row.group ? row.group.id : ch.id}"
        data-tip="${tipAttr(row.group
          ? `${row.group.name}\nA channel group of ${row.group.member_count} members.`
          : `${ch.name}\nThe channel this showing is on.`)}"
        >${esc(((row.group ? row.group.name : ch.name) || '?')[0])}</div>
      <div class="cc-body">
        <div class="cc-name ac-title">${highlight(row.title)}${
          row.sub_title ? `<span class="ac-sub">${highlight(row.sub_title)}</span>` : ''}</div>
        ${airingCardBadges(row)}
        ${rows ? `<div class="cc-rows">${rows}</div>` : ''}
      </div>
      <button class="cc-kebab" data-rowkebab="${ch.id}" data-air="${row.id}" type="button"
        aria-label="More for ${esc(row.title)}">&#8943;</button>
    </div>`;
  }

  /* ── The grain toggle ────────────────────────────────────────────────
     ONE builder for both slots, so the two spellings cannot drift apart. Which
     slot is on screen is CSS's decision, not this function's - the same seam every
     other dual-arrangement control on this page uses.

     Desktop is a tab strip sitting on a border that the results card then hangs
     off, which is what makes it read as "this list, in two shapes" rather than as
     navigation to another page (mockup 25, P1, settled round 4). Phone is a filled
     segmented control across the full width, deliberately NOT tabs: the page
     already has a tab strip at that width and that one navigates between PAGES
     (mockup 26). The strip lives INSIDE the results card, in place of the old
     "All Channels" heading (mockup 25, round 9). */
  /* The question in words, and the two answers as pills under it, at the TOP of the search
     zone. The four coordinates round 1 tried were all rejected for one reason: at every one
     of them the control that changes what a ROW IS carried the same weight as "all words /
     any word". The fix was to spend real estate rather than to move it again
     (dev/changelog/807).

     TITLE CASE AND A PICTORIAL GLYPH, both of which DESIGN.md §4 otherwise forbids and both
     of which it now carries a page-scoped exception for (dev/changelog/809, 810). The rule
     governs BUTTONS - a pill that names a mode is a label, not a verb, and it performs no
     work - and this page already ships three pictorial glyphs of its own. */
  const GRAIN_LABELS = { channels: 'Search Channels', airings: 'Search Programs (EPG)' };
  const GRAIN_ICONS = { channels: '&#128250;', airings: '&#128197;' };

  function renderGrainToggles() {
    const slot = $('#grainslot-panel');
    if (!slot) return;
    const keys = grainKeys();
    // A single implemented grain is not a choice, so it is not drawn as one. This is
    // what keeps the page honest against a catalog that only knows about channels.
    if (keys.length < 2) { slot.innerHTML = ''; return; }
    const pills = keys.map((k) => {
      const ui = GRAIN_UI[k];
      if (!ui) return '';
      const ico = GRAIN_ICONS[k]
        ? `<span class="gp-ico" aria-hidden="true">${GRAIN_ICONS[k]}</span>` : '';
      return `<button class="grainpill${state.grain === k ? ' on' : ''}" type="button"
        data-grain="${esc(k)}" aria-pressed="${state.grain === k}"
        data-tip="${tipAttr(ui.what)}">${ico}${esc(GRAIN_LABELS[k] || ui.tab)}</button>`;
    }).join('');
    slot.innerHTML = `<div class="grainask-lbl">What do you want to search?</div>
      <div class="grainpills" role="tablist">${pills}</div>`;
  }

  /* THE HEADING NAMES BOTH KINDS WHEN BOTH ARE PRESENT. "26 channels" over a list where
     three of the rows are groups is the one number in this shape that would be quietly
     wrong, and a number the user cannot explain is worse than no number
     (DESIGN-group-search-rows.md §5.2). The denominator on the count line is
     total_channels on both grains deliberately: it is the size of the library being
     searched, which does not change with the question asked. */
  function headCount() {
    // The exact total is still being counted (dev/changelog/598) - the rows already on
    // screen are a real, honest lower bound, so say that rather than nothing at all.
    if (last.total === null) {
      return last.rows.length ? `${nf(last.rows.length)}+ ${grainUi().noun}s` : '';
    }
    if (!last.total) return '';
    if (last.groupTotal && !isAirings()) {
      const c = last.channelTotal === null || last.channelTotal === undefined
        ? last.total - last.groupTotal : last.channelTotal;
      return `${nf(c)} channel${c === 1 ? '' : 's'} + ${nf(last.groupTotal)} `
        + `group${last.groupTotal === 1 ? '' : 's'}`;
    }
    return `${nf(last.total)} ${grainUi().noun}${last.total === 1 ? '' : 's'}`;
  }

  /* Split in two so the slow-request timer can repaint the RESULTS AREA alone. What
     is in here is everything read straight off `last` - the rows, the empty state, the
     loading state, the count heading, the header, the pager, the count line and the
     ticks. What is not is the rail and the bar: neither goes stale merely because a
     request is slow, and rebuilding the rail under a user who is mid-keystroke is how
     focus gets lost (BUGS.md 2026-07-31 09:42 PM). Every existing caller wants both and
     so calls renderResults(); the timer is the one caller that wants only this half. */
  function renderResultsArea() {
    const list = $('#ch-list');
    const empty = $('#all-empty');
    $('#all-loading').style.display = resultsPending ? '' : 'none';
    if (resultsPending) {
      /* The table STAYS, so the header keeps saying what is being loaded - an empty
         table with a spinner under it, rather than a bare card that says nothing about
         which grain you are now on. */
      list.innerHTML = '';
      empty.style.display = 'none';
      $('#atable').style.display = '';
      $('#all-loading-lbl').textContent = `Loading ${grainUi().noun}s…`;
    } else if (!last.rows.length) {
      // last.rows.length, not last.total: total is null while fetchCounts() is still
      // out (dev/changelog/598), and null is exactly as falsy as a genuine zero - the
      // row count is the one signal that is never pending once resultsPending is false.
      list.innerHTML = '';
      $('#atable').style.display = 'none';
      empty.style.display = '';
      // Say what was searched, not just that nothing matched: "no results" is
      // very often "you were only searching the name", and the scope control
      // lives inside the search box's own menu where it cannot be seen.
      // The way to widen it is a sheet at phone width, so the hint is TAPPABLE there
      // rather than pointing at a search box that no longer holds the control.
      const scope = fieldLabels().join(', ');
      empty.innerHTML = loadFailed
        /* A request that never answered is not the same fact as a search that matched
           nothing, and the no-match copy would be a claim about data nobody has. */
        ? `<p>The search could not be run.</p><div class="es-hint">${esc(loadFailed)}</div>`
        : `<p>No ${esc(grainUi().noun)}s match this search.</p><div class="es-hint">${
        state.q ? `Searching ${esc(scope)}. Widen it in ${isMobile()
          ? '<u id="es-scope">Searching in</u>.'
          : '<b>Search in</b> - click into the search box.'}`
                : 'No search text - your filters exclude everything.'}${
        standingHiddenTotal() ? ` &middot; ${nf(standingHiddenTotal())} hidden by your standing options` : ''}</div>`;
    } else {
      empty.style.display = 'none';
      $('#atable').style.display = '';
      // Four renderers, one dispatch: grain picks WHAT a row is, width picks how it
      // is drawn. Neither test is repeated inside the renderers.
      // Four renderers became six: the grain picks what a row is ABOUT, the width picks
      // how it is drawn, and on the channel grain the row's own `kind` picks which of the
      // two row kinds it is. Asked off the payload's `kind` rather than inferred from
      // which keys are present - a group's payload is deliberately a different shape.
      const phone = isMobile();
      const draw = isAirings()
        ? (phone ? airingCardHtml : airingRowHtml)
        : (r) => (r.kind === 'group'
          ? (phone ? groupCardHtml(r) : groupRowHtml(r))
          : (phone ? cardHtml(r) : rowHtml(r)));
      list.innerHTML = last.rows.map(draw).join('');
    }
    /* The heading is the COUNT and nothing else - the tab strip above it says which
       grain this is, and saying it twice is what round 3 deleted. The
       denominator is total_channels on both grains deliberately: it is the size of
       the library being searched, which does not change with the question asked. */
    $('#all-head-count').textContent = headCount();
    renderGrainToggles();
    // After the rows exist and before anything reads their geometry: the widest
    // name changes with the page and with the filter, so this is re-measured on
    // every render rather than once at load. Skipped while pending: measuring a
    // header with no rows under it would freeze the column to a meaningless width.
    if (!resultsPending) freezeNameCol();
    renderHead();
    renderPager();
    renderCount();
    // Rows are rebuilt whole, so the ticks, the header's select-all and the
    // "not in the current results" note are all restated from the Map here
    // rather than surviving in the DOM.
    updateSelectionUI();
    renderReplaceBar();
  }

  /* ── The replace action context ──────────────────────────────────────
     "You came here to replace scheduled recording X" - the second action context,
     and held to the same rules as add_to_group: it narrows nothing, it never becomes
     a chip, and it is dismissable (DESIGN-channel-search.md §3). What it changes is
     one row action: recording a showing while it is set also deletes the recording
     named here, through the record modal's existing replace_recording_id path.

     The NAME comes from CFG, resolved server-side - the page never renders a display
     name it was handed in a URL. */
  function renderReplaceBar() {
    const bar = $('#replace-bar');
    if (!bar) return;
    const on = state.replaceRec !== null && CFG.replaceRec;
    bar.style.display = on ? '' : 'none';
    if (!on) return;
    $('#replace-lbl').innerHTML = `Replacing <b>${esc(CFG.replaceRec.name)}</b>. `
      + 'Recording a showing below schedules it and deletes that one.';
  }

  function renderResults() {
    // Before anything reads the selection: these rows are the freshest answer there is
    // about the channels in it, and the bar's labels are derived from those facts.
    restampSelectionFromRows();
    renderResultsArea();
    // The standing card carries what each option hid - total/standingHidden arrive with
    // the rows on the channel grain, and separately via fetchCounts() on the airing grain
    // (dev/changelog/598); either way this call is what redraws the card once they do.
    renderRail();
    // The well's "N of M match what you typed" and the Search suggestion's own count are
    // read from last.total, so they are only right once it is known, same as above.
    renderBar();
  }

  function renderPager() {
    // Nothing has been paged yet, and the previous page numbers describe a list that
    // is no longer on screen. Prev/Next over a spinner would also be clickable.
    if (resultsPending) { $('#pager').innerHTML = ''; return; }
    // last.pages is null exactly when fetchCounts() is still out (dev/changelog/598) -
    // "Page 1 of 1" would be a real, wrong claim, not a pending one, so this is checked
    // explicitly rather than left to fall out of the arithmetic below.
    if (last.total === null) { $('#pager').innerHTML = ''; return; }
    const pages = Math.max(1, last.pages || 1);
    const sizes = pageSizeOptions();
    const turns = last.total > state.pageSize
      ? `<button class="btn btn-sm" data-pg="prev"${state.page <= 1 ? ' disabled' : ''}>&larr; Prev</button>
         <span class="pg-lbl">Page ${nf(state.page)} of ${nf(pages)}</span>
         <button class="btn btn-sm" data-pg="next"${state.page >= pages ? ' disabled' : ''}>Next &rarr;</button>`
      : '';
    // Shown whenever a smaller size would page, not only when this one does: at 250 with
    // 180 results there are no page turns, and hiding the size menu with them would leave
    // no way back to 100.
    const sizeMenu = last.total > Math.min(...sizes)
      ? `<select id="pg-size" class="form-control pg-size" aria-label="Rows per page">${sizes.map((n) =>
           `<option value="${n}"${n === state.pageSize ? ' selected' : ''}>${nf(n)} per page</option>`).join('')}
         </select>`
      : '';
    $('#pager').innerHTML = turns + sizeMenu;
  }

  /* The menu, plus whatever size a URL named that is not on it (`per_page=50` is a valid
     link), so the select never claims a size the page is not showing. */
  function pageSizeOptions() {
    const base = (CAT && CAT.page_size_options) || [100, 250, 500];
    return base.includes(state.pageSize)
      ? base : base.concat(state.pageSize).sort((a, b) => a - b);
  }

  /* ── Selection ───────────────────────────────────────────────────────
     THE SELECTION SURVIVES A FILTER CHANGE, A SORT AND A PAGE TURN
     (mockup 21 round 8): it is a Map of channel ids, not a scan of the rows on
     screen. What keeps that honest is the bar saying how many of the selected
     rows are currently out of view - without it, "Test selected (40)" on a page
     showing three ticks reads as a bug. */
  /* THE GUIDE ACTION IS ONE BUTTON, and which one depends on the selection. `in_guide` is
     per-channel, so a selection can carry both answers at once; the mixed case DISABLES
     the action and says why. Two always-visible Add/Remove
     buttons, and an Add that quietly applied to the not-yet-added half, were both offered
     and both rejected - a bulk action that silently acts on some of what you selected is
     the kind of quiet cleverness this app exists not to do. */
  const guideActionMode = () => {
    const n = selected.size;
    const inGuide = selectedInGuideIds().length;
    if (!n || !inGuide) return 'add';
    return inGuide === n ? 'remove' : 'mixed';
  };

  function renderGuideAction(n, short) {
    const mode = guideActionMode();
    const inGuide = selectedInGuideIds().length;
    // Remove, not Delete: the channel keeps everything except its row in the guide
    // (DESIGN.md 4). The short spelling drops "from guide" rather than reaching for a
    // glyph - the sanctioned set has no minus, and the one it does have reads as dismiss.
    $('#sel-add').textContent = mode === 'remove'
      ? (short ? `Remove (${nf(n)})` : `Remove selected from guide (${nf(n)})`)
      : (short ? `+ Guide (${nf(n)})` : `+ Add selected to guide (${nf(n)})`);
    $('#sel-add').disabled = mode === 'mixed';
    $('#sel-guide-note').textContent = mode === 'mixed'
      ? `${nf(inGuide)} of the ${nf(n)} selected channels already have their own guide row.`
        + ' Select channels that are all in, or all out, of your guide.'
      : '';
  }

  function updateSelectionUI() {
    const n = selected.size;
    const ctx = state.addToGroup !== null;
    // The action context keeps the bar up at zero selected, exactly as today's
    // Browse tab does: group-modal's fixedGroup mode still offers suggestions
    // with an empty selection, so there is something to do here.
    const up = n > 0 || ctx;
    $('#sel-bar').classList.toggle('show', up);
    /* The bar is FIXED at phone width, not sticky, so the pager underneath has to
       be given room to clear it rather than ending up behind it. */
    document.body.classList.toggle('has-selbar', up);
    /* Short labels below the breakpoint. This is what lets all three actions stay
       real buttons at 375 instead of one going behind a kebab: the labels were
       what did not fit, not the actions. */
    const short = isMobile();
    $('#sel-group').textContent = short ? `+ Group (${nf(n)})` : `+ Group selected (${nf(n)})`;
    renderGuideAction(n, short);

    /* The delete action is offered only when the selection holds channels the provider has
       stopped listing, and counts THOSE rather than the whole selection - a button reading
       the same number as the other three would promise to delete rows it will refuse. */
    $('#sel-hide').textContent = short ? `Hide (${nf(n)})` : `Hide selected (${nf(n)})`;

    /* Un-hide counts the hidden subset for the same reason Delete counts the missing one:
       a button reading the whole selection's number would promise to act on rows it has
       nothing to do. */
    const nHidden = selectedHiddenIds().length;
    const unhideBtn = $('#sel-unhide');
    unhideBtn.style.display = nHidden ? '' : 'none';
    unhideBtn.textContent = short ? `Un-hide (${nf(nHidden)})` : `Un-hide selected (${nf(nHidden)})`;

    const nMissing = selectedMissingIds().length;
    const delBtn = $('#sel-delete');
    delBtn.style.display = nMissing ? '' : 'none';
    delBtn.textContent = short ? `Delete (${nf(nMissing)})` : `Delete missing (${nf(nMissing)})`;

    const onPage = new Set(pageChannels().map((c) => c.id));
    const away = Array.from(selected.keys()).filter((id) => !onPage.has(id)).length;
    /* Two facts, and the group one is said whenever group rows are on screen rather than
       only when something is selected: every button on this bar is a channel action, so a
       user who selected everything and still sees group rows unticked is owed the reason
       (DESIGN-group-search-rows.md §5.2 - the action set is subtraction, and subtraction
       has to say what it took). */
    const notes = [];
    if (away) {
      notes.push(`${nf(away)} selected channel${away === 1 ? '' : 's'} not in the current results`);
    }
    // Only while there IS a selection, which is also the only time this bar is up: the
    // sentence answers "why did selecting everything leave those rows unticked", and it
    // has nothing to say to someone who has selected nothing.
    if (n && last.groupTotal && !isAirings()) {
      notes.push('Group rows are not selectable - every action here is a channel action.');
    }
    $('#sel-note').textContent = notes.join(' · ');

    $('#sel-ctx').style.display = ctx ? '' : 'none';
    if (ctx) {
      // Resolved from the id through the catalog, never from a group_name in the
      // URL - the page does not have to trust a display name it was handed
      // (DESIGN-channel-search.md 2.4).
      $('#sel-ctx-lbl').textContent = `Adding to "${addToGroupName()}"`;
      $('#sel-ctx-add').textContent = `+ Add Channels${n ? ` (${nf(n)})` : ''}`;
    }

    $$('#ch-list input[type=checkbox]').forEach((cb) => {
      cb.checked = selected.has(Number(cb.dataset.id));
    });
    const all = $('#all-select-all');
    const page = pageChannels();
    if (all) all.checked = page.length > 0 && page.every((c) => selected.has(c.id));
  }

  function addToGroupName() {
    const g = (CAT ? CAT.groups : []).find((x) => x.id === state.addToGroup);
    return g ? g.name : `Group #${state.addToGroup}`;
  }
  /* The account colour and name ride along for the group-create flow's picked-channel
     list, which shows a provenance dot per row. Same bargain as the three stamped facts
     below: they describe the row as it was when it was ticked, and they decide only what
     a modal DRAWS - never what the server does with the ids. */
  const selectedChannels = () =>
    Array.from(selected.entries()).map(([id, v]) => ({
      channel_id: id,
      channel_name: v.name,
      account_color: v.account_color || '',
      account_name: v.account_name || '',
    }));

  function setRowSelected(id, on) {
    const ch = channelOnPage(id);
    /* `lifecycle` is carried alongside the name because the selection outlives the rows
       it came from - a page turn away, the row payload that knew this channel was missing
       is gone, and the delete action has to stay offered for it. It decides what the bar
       OFFERS and nothing more: the server re-derives missing-ness on both the preview and
       the delete, so a stale value here can never delete something it should not. */
    if (on) {
      selected.set(id, {
        name: ch ? ch.name : `Channel #${id}`,
        lifecycle: ch ? ch.lifecycle : null,
        // Carried for the same reason as `lifecycle`, and used the same way: it decides
        // whether Un-hide is OFFERED, never what the server does. The server re-reads
        // every channel's real state, so a stale value here cannot mis-hide anything.
        hidden: ch ? !!ch.hidden : false,
        /* `in_guide` is "has a guide row of its OWN" and nothing else (dev/changelog/751),
           which is the question the guide action asks: adding gives a channel its own row
           whether or not a group already puts its listings on screen. The wider "is it in
           the guide at all" is `guide_via`, and it is deliberately NOT consulted here -
           two spellings of "already in the guide" is what dev/changelog/791 split apart. */
        in_guide: ch ? !!ch.in_guide : false,
        account_color: ch && ch.account ? ch.account.color : '',
        account_name: ch && ch.account ? ch.account.name : '',
      });
    } else {
      selected.delete(id);
    }
  }

  const selectedMissingIds = () =>
    Array.from(selected.entries()).filter(([, v]) => v.lifecycle === 'missing').map(([id]) => id);

  const selectedHiddenIds = () =>
    Array.from(selected.entries()).filter(([, v]) => v.hidden).map(([id]) => id);

  const selectedInGuideIds = () =>
    Array.from(selected.entries()).filter(([, v]) => v.in_guide).map(([id]) => id);

  /* The three facts above are stamped when a row is TICKED, so anything that rewrites a
     channel afterwards - this page's own add/remove, a hide, or a plain refetch that picks
     up a change made elsewhere - leaves them describing the row as it used to be. That is
     visible: after a bulk add, the guide action would still be offering to add. So every
     time rows land, the selected channels among them re-stamp from the new payload. Rows
     NOT on the current page keep what they had, which is the same bargain the stamp made
     in the first place - it decides what the bar OFFERS, and the server re-derives the
     truth on every action. */
  function restampSelectionFromRows() {
    pageChannels().forEach((ch) => {
      const v = selected.get(ch.id);
      if (!v) return;
      v.lifecycle = ch.lifecycle;
      v.hidden = !!ch.hidden;
      v.in_guide = !!ch.in_guide;
      v.account_color = ch.account ? ch.account.color : '';
      v.account_name = ch.account ? ch.account.name : '';
    });
  }

  const standingHiddenTotal = () =>
    Object.values(last.standingHidden || {}).reduce((a, b) => a + b, 0);
  const fieldLabels = () =>
    (CAT ? CAT.fields.filter((f) => state.fields.includes(f.key)).map((f) => f.label) : []);

  /* The trailing "Reason: ..." line both decline surfaces carry, spelled once. The server's
     own sentence wins; `last.degraded` is the fallback for a response predating
     `declined_reason` (and for the one path that has a reason without a decline). Returns a
     leading blank line or nothing at all, so the caller can concatenate unconditionally. */
  const whyDeclined = () => {
    const why = declinedWhy || last.degraded || '';
    return why ? `\n\nReason: ${why}` : '';
  };

  function renderCount() {
    const total = last.total;
    /* Missing and STILL COMING, which is the only one of the three that earns "counting..."
       - a declined or failed total is missing and not coming (dev/changelog/676). */
    const countsPending = total === null && !countsDeclined && !countsFailed;
    const countsMissing = total === null;
    const from = (state.page - 1) * state.pageSize + 1;
    const to = countsMissing ? last.rows.length : Math.min(total, state.page * state.pageSize);
    const parts = [];
    /* "No matches" is a claim about data nobody has yet, and the previous range is
       about a question no longer on screen. Only the three response-derived parts are
       gated here - the sort note, the scope line and the DUP back button below are
       derived from state and stay true throughout.

       COUNTSPENDING is its own branch, not folded into the `!total` "no matches" case:
       total is null while fetchCounts() is still out (dev/changelog/598), and the rows
       already on screen prove there IS at least one match, so saying "No matches" would
       be actively wrong rather than merely incomplete.

       DECLINED AND FAILED ARE TWO MORE BRANCHES, for the same reason and a stronger one:
       the total is not coming at all, so "counting the total..." would promise work nobody
       is doing. Each says which it is and why, because a number the user cannot explain is
       worse than no number - and "not counted" with a reason is a number they can explain.
       The rows themselves are complete and correct in every one of these states. */
    parts.push(resultsPending ? 'Searching&hellip;'
      : countsPending ? `Showing these <strong>${nf(to)}</strong> - counting the total&hellip;`
      : countsDeclined ? `Showing these <strong>${nf(to)}</strong> <span class="uncounted" data-tip="${tipAttr(
        'The total was not counted: counting every match would cost seconds here, so the '
        + 'matches themselves were returned instead.\n\nThe rows shown are complete and '
        + 'correct.' + whyDeclined())}">- total not counted</span>`
      : countsFailed ? `Showing these <strong>${nf(to)}</strong> <span class="uncounted" data-tip="${tipAttr(
        `The total could not be counted.\nReason: ${countsFailed}`
        + '\n\nThe rows shown are complete and correct.')}">&#9888; total not counted</span>`
      : (total
        ? `Showing <strong>${nf(from)}-${nf(to)}</strong> of <strong>${nf(total)}</strong> match${total === 1 ? '' : 'es'}`
        : '<strong>No</strong> matches'));
    if (CAT) parts.push(`<span class="sep">&middot;</span> ${nf(CAT.total_channels)} channels total`);

    /* WHAT THIS SHAPE COSTS THE COUNTS, said here rather than left to be noticed. A group
       row is a different table with a different primary key, so it is counted in `total`
       and nowhere else: not in the "channels total" beside this, and not on the facet rail.
       A single blended number would be the defect (DESIGN-group-search-rows.md §5.2), so
       the line names the two and explains the one that is missing. */
    if (!resultsPending && last.groupTotal) {
      const n = last.groupTotal;
      parts.push(`<span class="sep">&middot;</span> <span class="sortnote" data-tip="${tipAttr(
        `${nf(n)} of these rows ${n === 1 ? 'is a channel GROUP' : 'are channel GROUPS'}, not `
        + 'channels.\n\nEvery facet count on the left, and the "channels total" beside this, '
        + 'is defined over the channels table. A group is a different table, so it is counted '
        + 'here and nowhere else.')}">${nf(n)} group row${n === 1 ? '' : 's'}</span>`);
    }

    /* THE SORT A GROUP CANNOT ANSWER. `name` and `health` cross - a group has both - but
       category, account, stream id, EPG id and URL describe a stream or a provider, and a
       group has none of them. So they land first, in name order, and the line says so
       rather than leaving the reader to work out why five rows ignored the sort. */
    if (!resultsPending && last.groupTotal && !isAirings() && !GROUP_SORTS.has(state.sort)) {
      parts.push(`<span class="sep">&middot;</span> <span class="sortnote" data-tip="${tipAttr(
        `A channel group has no ${sortLabel(state.sort, state.grain).toLowerCase()} of its own `
        + '- that describes a stream or a provider, and a group is a set of them. Group rows '
        + 'are listed first, in name order, instead of being sorted by a value they do not '
        + 'have.\n\nSort by Channel or Health and they sort in among the channels.')
      }">groups first</span>`);
    }

    /* THE SORT COULD NOT COME WITH YOU. Set by setGrain() and cleared the moment
       you pick a sort yourself. Said out loud because a list that quietly reordered
       itself is the silent behavior this project's founding principle forbids - and
       the alternative (letting the engine 400 a cross-grain sort) would turn an
       ordinary tab click into an error page. */
    if (state.sortNote) {
      parts.push(`<span class="sep">&middot;</span> <span class="sortnote" data-tip="${tipAttr(
        state.sortNote.why)}">${esc(state.sortNote.text)}</span>`);
    }

    /* THE DISCLOSURE RULE. Every standing option that actually removed a row
       says so BY NAME with its own count, and clicking it puts those rows back.
       One entry per option, never a merged "N hidden" - a lump sum tells you
       something vanished but not what to do about it. This is the whole reason
       an option that hides by default is allowed to exist.

       BY NAME is the option's own noun, not the word "hidden" (dev/changelog/778).
       Repeating one verb made the line carry no information the moment a second
       option was on: "849 hidden - 752 hidden - 101,916 hidden - 33,401 hidden"
       is four facts wearing one label, and every option added makes it worse. */
    standingFor().forEach((s) => {
      // countsMissing, not countsPending: a declined or failed breakdown has no per-option
      // numbers either, and `standingHidden` is stale from the previous search if anything.
      if (resultsPending || countsMissing) return;
      const n = (last.standingHidden || {})[s.key];
      if (!n) return;
      parts.push(`<span class="sep">&middot;</span> <span class="hid" data-standing-off="${s.key}" data-tip="${tipAttr(
        `${nf(n)} ${grainUi().noun}${n === 1 ? '' : 's'} left out of this search by the `
        + `standing option "${s.label}".\n\nClick to put them back.`)}">${nf(n)} ${esc(s.noun || 'hidden')}</span>`);
    });

    // What states the scope once the box loses focus - Search in lives inside
    // the box's own popover. Only while there is text: with an empty box the
    // scope is matching nothing, and saying so would be noise.
    if (state.q) {
      // Same job at both widths, different destination: up here the control is in the
      // search box's own menu, so this puts the caret back there; down there it is a
      // sheet, and pointing at a box that no longer holds the control would be a dead end.
      parts.push(`<span class="sep">&middot;</span> <span class="scope-say" id="cs-scope" data-tip="${tipAttr(
        `Searching ${fieldLabels().join(', ')}.\n\n${isMobile()
          ? 'Tap to open Searching in.'
          : 'Click to put the caret back in the search box, where Search in lives.'}`)}">searching ${esc(fieldLabels().join(', '))}</span>`);
    }
    // A degraded search is correct but slower. Reported, never merely logged.
    if (last.degraded && !resultsPending) {
      parts.push(`<span class="sep">&middot;</span> <span class="degraded" data-tip="${tipAttr(
        `This search ran without its index and is slower than usual.\nReason: ${last.degraded}\nResults are the same.`)}">&#9888; unindexed</span>`);
    }
    // A row refresh that failed (most often a timeout on a broad filter) leaves the OLD
    // rows on screen rather than claiming nothing matched - but nothing else here said
    // the CURRENT filters were never actually applied to what's showing. The toast that
    // fires on failure is transient and easy to miss; this is the persistent version of
    // the same fact (BUGS.md 2026-08-07, tag filter on a broad search).
    if (loadFailed && !resultsPending && last.total) {
      parts.push(`<span class="sep">&middot;</span> <span class="stale-fail" data-tip="${tipAttr(
        `These are the results from before your last change - the search could not be `
        + `refreshed.\nReason: ${loadFailed}`)}">&#9888; not refreshed</span>`);
    }
    // The way back out of a DUP drill-in. It is only ever here because something
    // replaced the user's search for them, so it stays until it is used.
    if (searchSnapshot) {
      parts.push('<button class="cs-back" id="cs-back" data-tip="Put back the search and filters '
                 + 'you had before you clicked that DUP badge.">&larr; Your search</button>');
    }
    $('#cs-count').innerHTML = parts.join(' ');

    // The two indicators inside the box: how many this matches, and which bit
    // of syntax is currently doing something.
    const parsed = parseQuery(state.q);
    // Blank rather than the previous total: this one sits inside the box being typed
    // in, which is exactly where a stale number reads as an answer to what was typed.
    // Blank whenever the total is missing for ANY reason, for the same reason - nf(null)
    // would print "0", and a "0" inside the box being typed in reads as a real answer.
    $('#scount').textContent =
      (!resultsPending && !countsMissing && (state.q || state.filters.length)) ? nf(total) : '';
    const bits = [];
    if (parsed.ex.length) bits.push(`excluding ${parsed.ex.length}`);
    if (parsed.inc.some(hasWildcard)) bits.push('wildcard');
    $('#syn').style.display = bits.length ? 'inline' : 'none';
    $('#syn').textContent = bits.join(' + ');
    $('#sb-clr').style.display = state.q ? '' : 'none';
  }

  /* ── The proactive degraded-search notice ────────────────────────────
     Says search is slow RIGHT NOW, before anything is typed. The `unindexed` badge in
     renderCount() above is a different region with a different trigger - it reports on a
     response that already came back - and the two are deliberately both present: one is
     the standing condition, the other is per-result detail. Do not merge them.

     Fed from two places, and this function is the region's ONLY writer: the
     /api/nav-status poll in base.html (so it appears and clears on its own across a
     sync's lifetime, at display.nav_poll_interval_seconds), and applyNow(), because
     turning on a program field while a sync is running has to warn immediately rather
     than at the next poll tick. dev/changelog/427. */
  let readiness = null;

  /* Which of the server's two answers applies to what is currently being searched.
     MIRRORS app/channel_search.py::_index_names(): any field sourced from a program
     column needs both indexes, everything else needs the channel index alone. Read off
     the catalog's own `source` rather than a re-typed list of field keys, so a field
     added to the registry is classified here without a second edit. The `programs` key
     is NOT the program index on its own - it is the both-indexes answer. */
  function readinessNow() {
    if (!readiness || !CAT) return null;
    const byKey = Object.fromEntries(CAT.fields.map((f) => [f.key, f]));
    // 'program' is the WIRE value (channel_search.SOURCE_PROGRAM) - compared as served,
    // the same way line 1027 already does it.
    const needsPrograms = state.fields.some((k) => byKey[k] && byKey[k].source === 'program');
    return readiness[needsPrograms ? 'programs' : 'channels'] || null;
  }

  function renderSearchReadiness() {
    const el = $('#cs-degraded');
    if (!el) return;
    const answer = readinessNow();
    if (!answer || answer.ready) { el.style.display = 'none'; el.innerHTML = ''; return; }
    /* Only claims what is still true (dev/changelog/676, 678, 681). Rows are the one thing
       still guaranteed the same - the unindexed path is slower but correct. Totals and
       filter counts are NOT: a degraded search now declines them rather than paying for
       them (676), and a queued search can be shed or superseded outright rather than
       always running to completion (678) - so "will typically resolve itself" overclaimed
       what a single search does, even though the STANDING CONDITION genuinely does clear on
       its own now, bounded by the index janitor (680) as well as the next sync.

       The headline names NO cause on purpose (2026-08-01). A sync is the common
       case - it moves the source watermark 8x/day and every search in that window is a
       1.9M-row scan - but it is one of four states readiness can report, the others being
       never built, mid-rebuild, and last rebuild failed. Naming the sync in the headline
       would put a false statement on screen for the other three, so the headline states
       only what is true in all four, and the reason line below names which one it is - the
       same server-supplied-reason pattern the count line and facet rail use for a decline. */
    el.innerHTML = `&#9888; Searches are running without their index right now. Rows that `
      + `come back are complete and correct, but totals and filter counts may not be `
      + `available, and a search can be slower than usual or fail to complete. This clears `
      + `once the index next rebuilds - normally at the end of an account sync, or `
      + `automatically otherwise.`
      + `<span class="csd-why">${esc(answer.reason || '')}</span>`;
    el.style.display = '';
  }

  window.__applySearchReadiness = (payload) => {
    readiness = payload || null;
    renderSearchReadiness();
  };

  /* ── Row actions ─────────────────────────────────────────────────────
     Everything here reuses what ships today rather than adding an endpoint:
     POST /api/guide/channels/<id>/add for the guide, the /channels/<id>/toggle
     form for the removal, and the three shared modals. */

  /* Adding a channel to the guide changes which copy of a duplicate cluster is
     KEPT (in-guide is the first rung of the cascade), so the page re-runs the
     search rather than flipping the row in place - a stale KEPT badge would be
     wrong about the one thing the badge exists to say. Same page, no scroll. */
  /* Its ONE caller is the phone row sheet, which closes before this runs, so there is no
     button left to put into a pending state - the toast is the whole feedback. The
     desktop's per-row guide button, which is what the in-place pending state was for, is
     gone (dev/changelog/860). */
  function addRowToGuide(id) {
    jsonFetch(`${CFG.addGuideUrlBase}${id}/add`, { method: 'POST' })
      .then((data) => {
        if (!data.duplicate_warning) {
          showToast(`"${data.channel_name || 'Channel'}" added to your guide.`);
          applyNow(false);
          return;
        }
        // The warning names what it clashes with, because "add anyway?" without
        // the other channel's name is a question nobody can answer.
        const names = (data.conflicting_channels || []).map((c) => c.name).join(', ');
        if (!window.confirm(`"${data.channel_name}" shares a stream URL with a channel already `
                            + `in your guide:\n\n${names}\n\nAdd it anyway?`)) return;
        return jsonFetch(`${CFG.addGuideUrlBase}${id}/add?force=1`, { method: 'POST' })
          .then(() => { showToast(`"${data.channel_name}" added to your guide.`); applyNow(false); });
      })
      .catch((e) => showToast(e.message || 'Could not add that channel.', { type: 'error' }));
  }

  /* The same endpoint the bulk action posts to, with one id in the list. It used to
     synthesize a form POST to /channels/<id>/toggle and let the redirect reload the whole
     page, because no JSON endpoint for removal existed - which cost the selection and a
     scroll position on every single-row remove (dev/changelog/792). Re-runs the search
     for the same reason adding does: in-guide is the first rung of the duplicate-KEPT
     cascade, so a row flipped in place would leave a stale KEPT badge on screen. */
  function removeRowFromGuide(id) {
    jsonFetch(CFG.removeGuideUrl, {
      method: 'POST', body: JSON.stringify({ channel_ids: [id] }),
    })
      .then(() => { showToast('Removed from your guide.'); applyNow(false); })
      .catch((e) => showToast(e.message || 'Could not remove that channel.', { type: 'error' }));
  }

  /* ── The DUP drill-in, and the way back ──────────────────────────────
     THE DRILL-IN IS BY CHANNEL ID, not by searching the stream URL the way the
     mockup did: the row payload masks the URL (it routinely carries account
     credentials), so the text on screen is not the text in the database and
     searching it would match nothing. `dup.ids` carries the whole cluster and
     the engine already has the hidden `chan` dimension.

     Show duplicates has to come ON for the drill-in, or the losers it is about
     to show would be the very rows that are hidden without it. Getting back out of
     a drill-in is critical, so the way in records the way back before it changes
     anything. */
  /* A SEARCH IS ITS URL, and that string is the only representation of one this
     page keeps. "Put that exact search back" is asked twice - by this drill-in's
     way back, and by a saved search - so both spell it the same way rather than
     carrying a second object shape that could disagree about, say, whether the
     standing options travel with it. toParams() writes it and parseState() reads
     it, and those two ARE the URL contract (DESIGN-channel-search.md §2), so
     there is no third parser to keep in step.

     `facets` is deliberately not in it: that parameter says which counts one
     REQUEST asked for, not what the search is. */
  let searchSnapshot = null;                 // a query string, or null

  const snapshot = () => toParams().toString();

  function restore(params) {
    // The action context is an errand, not part of the search: coming back out of
    // a drill-in, or loading a saved search, changes what is listed and never what
    // the user came here to do.
    const context = state.addToGroup;
    parseState(params);
    state.addToGroup = context;
    // Same reason as at boot: a restored `when`/`duration` window has to put its controls
    // back, not only its chip.
    hydrateWhenControls();
    hydrateDurationControls();
    qbox.value = state.q;
    $('#sb-clr').style.display = state.q ? '' : 'none';
    // A saved search or a drill-in can carry a different grain, and the toggle, the
    // column tracks and the picker all follow the grain rather than the state generally.
    renderGrainToggles();
    renderColsPop();
    applyColumns();
    applyNow();
  }

  function dupDrillIn(id) {
    const row = channelOnPage(id);
    if (!row || !row.dup) return;
    if (!searchSnapshot) searchSnapshot = snapshot();
    state.q = '';
    qbox.value = '';
    $('#sb-clr').style.display = 'none';
    state.filters = [{ key: 'chan', values: row.dup.ids.map(String), ex: [] }];
    // ADD, not delete: `showdup` present is what shows every copy. Drilling into a cluster
    // and then hiding all but one of its members would show a single row and call it the
    // cluster (dev/changelog/778).
    state.standing.add('showdup');
    applyNow();
    showToast(`Showing the ${row.dup.count} channels that share that stream URL. `
              + 'Use "← Your search" to return.');
  }

  /* ── The row kebab sheet ─────────────────────────────────────────────
     The card has no per-row "+ Add to Guide" button (mockup 22 round 2:
     "get rid of it altogether and make channel interactions happen through the
     checkboxes ... or at least make it only a ... button"). It is both halves of
     that in the end: the checkbox plus the bottom bar is the path whether the
     selection is one channel or fifty, and this holds the actions that are about
     ONE channel and had nowhere else to live.

     Nothing became unreachable by losing the button - guide membership still
     reads off the card tint and the In Guide badge, and adding or removing is the
     first thing in here. */
  let rowSheetId = null;
  let rowSheetAir = null;

  const findAiring = (airId) => (isAirings() ? last.rows.find((r) => r.id === airId) : null);

  /* THE RECORD ACTION IS FIRST WHEN THE ROW IS A SHOWING, and at phone width it is the
     ONLY place the action exists ("P4 - kebab only", mockup 26 round 8) - so its labels
     are the full sentences the desktop button shortens. The card still shows the STATE
     through recBadge(), because a state readable only behind a tap is not a state the
     list shows. */
  const REC_SHEET_LABELS = {
    recording: 'Manage this recording',
    scheduled: 'Edit this scheduled recording',
    recorded: 'Record this showing again',
    past: 'This showing has ended',
    none: 'Record this showing',
    replace: 'Record this instead, and delete the original',
  };

  function rowRecordSheetHtml(air) {
    if (!air) return '';
    const dead = air.record_state === 'past';
    const a = recAction(air);
    const start = utcDate(air.start_time);
    const label = (a === REC_ACTION_REPLACE)
      ? REC_SHEET_LABELS.replace
      : (REC_SHEET_LABELS[air.record_state] || REC_SHEET_LABELS.none);
    return `<div class="prow drill${dead ? ' off' : ''}"${dead ? '' : ` data-act="${a.act}"`}
        data-air="${air.id}" data-id="${air.channel.id}">
      <span class="pico">${dead ? '&#8709;' : '&#9679;'}</span>
      <span class="pv">${esc(label)}</span>
      <span class="hint">${esc(dayLabel(start))} ${start ? esc(fmtTimeTz(start)) : ''}</span></div>`;
  }

  function rowSheetBody() {
    const air = rowSheetAir === null ? null : findAiring(rowSheetAir);
    const ch = air ? air.channel : channelOnPage(rowSheetId);
    if (!ch) return '';
    const sel = selected.has(ch.id);
    return `${rowRecordSheetHtml(air)}
      ${air ? `<div class="sh-note" style="padding:2px 0 8px">On <b>${esc(ch.name)}</b>, ${
        esc(dayLabel(utcDate(air.start_time)))} ${esc(timeRange(air))}.</div>` : ''}
      ${rowSheetChannelRows(ch, sel)}`;
  }

  function rowSheetChannelRows(row, sel) {
    return `<div class="prow drill" data-act="open-channel" data-id="${row.id}">
        <span class="pico">&#8599;</span><span class="pv">Open channel detail</span></div>
      ${row.in_guide
        ? `<div class="prow drill" data-act="in-guide" data-id="${row.id}">
            <span class="pico">&#10003;</span><span class="pv">Remove from the TV Guide</span></div>`
        : `<div class="prow drill" data-act="add-guide" data-id="${row.id}">
            <span class="pico">&#43;</span><span class="pv">Add to the TV Guide</span>
            ${(row.guide_via || []).length
              ? `<span class="hint">already in it via ${esc(row.guide_via[0].name)}</span>` : ''}</div>`}
      <div class="prow drill" data-rowtest="${row.id}">
        <span class="pico">&#9889;</span><span class="pv">Test this channel</span></div>
      <div class="prow drill" data-rowsel="${row.id}">
        <span class="pico">${sel ? '&#10005;' : '&#10003;'}</span>
        <span class="pv">${sel ? 'Deselect this channel' : 'Select this channel'}</span></div>
      ${row.dup ? `<div class="prow drill" data-act="dup-badge" data-id="${row.id}">
        <span class="pico">&#9282;</span>
        <span class="pv">${nf(row.dup.count)} channels share this stream URL</span></div>` : ''}
      <div class="sh-note">Selecting from in here and ticking the card's checkbox write the same
        selection, so a one-channel action and a fifty-channel action are one path rather than
        two.${isAirings() ? ' <b>Selecting a showing selects its channel</b> - several showings '
        + 'on one channel are one selection, on both tabs.' : ''}</div>`;
  }

  function openRowSheet(id, airId = null) {
    rowSheetId = id;
    rowSheetAir = airId;
    const air = airId === null ? null : findAiring(airId);
    const ch = air ? air.channel : channelOnPage(id);
    if (!ch) return;
    // Truncated HERE and nowhere else: a sheet head is one line by construction,
    // and the card is where the full name is guaranteed. On the airing grain the sheet
    // is about the SHOWING, so it is the program that names it and the channel is a
    // line inside.
    const head = air ? air.title : ch.name;
    const title = head.length > 34 ? `${head.slice(0, 34)}…` : head;
    const sheet = openSheet({
      kind: 'row', title, body: rowSheetBody(),
      redraw: () => { sheetBody().innerHTML = rowSheetBody(); },
    });
    sheet.addEventListener('click', (e) => {
      const sel = e.target.closest('[data-rowsel]');
      if (sel) {
        // Through the same Map the checkbox writes, so the card's tick, this row
        // and the bottom bar cannot disagree about what is selected.
        const rid = Number(sel.dataset.rowsel);
        setRowSelected(rid, !selected.has(rid));
        renderResults();
        renderOpenSheet();
        return;
      }
      const test = e.target.closest('[data-rowtest]');
      if (test) {
        closeSheet();
        testChannels([Number(test.dataset.rowtest)]);
        return;
      }
      const act = e.target.closest('[data-act]');
      if (!act) return;
      const rid = Number(act.dataset.id);
      // Everything else here finishes the sheet's job, so the sheet goes with it -
      // otherwise the toast, or the sheet that replaces it, lands behind this one.
      if (act.dataset.act === 'dup-badge') { openDupSheet(rid); return; }
      closeSheet();
      if (act.dataset.act === 'add-guide') addRowToGuide(rid);
      else if (act.dataset.act === 'in-guide') removeRowFromGuide(rid);
      else if (act.dataset.act === 'open-channel') location.href = `${CFG.channelUrlBase}${rid}`;  // nav-ok: phone bottom-sheet button, a tap
      else if (REC_ACT_KEYS.has(act.dataset.act)) openRecordModal(Number(act.dataset.air), null);
    });
  }

  /* A GROUP's kebab sheet: the same subtraction its card makes, in sheet form. No
     selection row - the selection bar is channel actions and a group answers none of them -
     no Test, and no guide action, which went from every surface of this page together
     (dev/changelog/860). Whether the group is in the guide is still on the card, as the
     `In Guide` badge; changing it is a decision made on the group's own page, which the
     one row here opens. */
  function groupOnPage(id) {
    return (last.rows || []).find((r) => r.kind === 'group' && r.id === id) || null;
  }

  function openGroupSheet(id) {
    const g = groupOnPage(id);
    if (!g) return;
    const title = g.name.length > 34 ? `${g.name.slice(0, 34)}…` : g.name;
    const sheet = openSheet({
      kind: 'group',
      title,
      body: `<div class="prow drill" data-act="open-group" data-id="${g.id}">
          <span class="pico">&#8599;</span><span class="pv">Open the group</span></div>
        ${g.serving
          ? `<div class="prow"><span class="pico">&#9673;</span>
              <span class="pv">Would record from ${esc(g.serving.name)}</span></div>`
          : `<div class="prow"><span class="pico">&#9888;</span>
              <span class="pv">No recording-enabled member</span></div>`}
        <div class="sh-note">${nf(g.member_count)} channel${g.member_count === 1 ? '' : 's'}${
          g.recording_member_count === g.member_count
            ? ', all recording-enabled'
            : `, ${nf(g.recording_member_count)} of them recording-enabled`}. A group is not
          selectable here - Test, Add to guide, Group and Hide are all channel actions.</div>`,
    });
    sheet.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]');
      if (!act) return;
      closeSheet();
      location.href = `${CFG.groupDetailUrlBase}${Number(act.dataset.id)}`;  // nav-ok: phone bottom-sheet button, a tap
    });
  }

  /* ── Opening the shared Schedule Recording modal ──────────────────────
     The SAME modal the TV Guide opens, through the same guide.js::openModal - not a
     second schedule modal on this page. What it needs and the search response does not
     carry is fetched here, per CLICK: the raw stream URL (the row payload masks it, and
     a page of 100 rows on every keystroke is the wrong place for 100 credentials), the
     recording's profile, and the group behind a group-backed recording.

     One request per click is the trade, and it is the right way round: the click is
     rare, the keystroke is not. */
  const REC_ACT_KEYS = new Set(['rec-new', 'rec-edit', 'rec-manage', 'rec-again']);

  /* Whether the showing behind an id already carries a recording. Read from the row rather
     than from the button that was clicked, because the kebab sheet and the desktop button
     are two paths into openRecordModal and only one of them has a label to read. */
  function recordedAlready(airId) {
    const air = findAiring(airId);
    return !!(air && air.record_state && air.record_state !== 'none');
  }

  // Was the modal that is open right now opened to REPLACE? Set on every open, read once
  // by the save callback - a save is the only thing that consumes the replace context.
  let pendingReplace = false;

  function openRecordModal(airId, btn) {
    if (!airId) return;
    if (typeof openModal !== 'function') {
      showToast('The scheduling modal did not load on this page.', { type: 'error' });
      return;
    }
    const label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Opening…'; }
    // The replace context rides the SAME modal path the TV Guide uses - openModal's third
    // argument writes #modal-replace-id, which new_recording_json reads as
    // replace_recording_id. Sent only for a showing that has no recording of its own, which
    // is the only state the Replace label is offered in.
    const replacing = state.replaceRec !== null && !recordedAlready(airId);
    const opts = replacing ? { replaceRecId: state.replaceRec } : {};
    // Reset on EVERY open, not only on a replacing one: opening the modal on some other
    // row is what tells us the replace the user started was not the one they saved.
    pendingReplace = replacing;
    /* THE GROUP IS NAMED BY THE ROW, never inferred by the endpoint. A row that stands for
       a group says so on screen - the Group pill, the stacked tile, the accent edge - so
       Record on it has to schedule the GROUP, or the row lied. Every other Record click
       sends no group at all and gets a plain single-channel recording, which is the
       distinction dev/changelog/793 settled and this preserves (dev/changelog/811). */
    const air = findAiring(airId);
    const groupId = air && air.group && !air.group.check_only ? air.group.id : null;
    const url = `${CFG.recordContextUrlBase}${airId}/record-context`
      + (groupId ? `?group=${groupId}` : '');
    jsonFetch(url)
      .then((data) => { openModal(data.program, data.channel, opts); })
      .catch((e) => showToast(e.message || 'Could not open that showing.', { type: 'error' }))
      .finally(() => { if (btn) { btn.disabled = false; btn.textContent = label; } });
  }

  /* Called by the record modal's own save/delete path (window.onScheduleSaved, set in
     the template). The search is re-run rather than the page reloaded: the only thing
     that changed is one row's record_state, and a reload would throw away the search
     that is on screen.

     A REPLACE consumes its context: the recording it named has just been deleted by the
     save, so leaving the bar up would name a row that is gone and arm every other Record
     button to delete an id that no longer exists. */
  window.channelSearchRefresh = () => {
    if (pendingReplace) {
      pendingReplace = false;
      state.replaceRec = null;
      renderReplaceBar();
      showToast('Replaced. The original scheduled recording has been deleted.');
    }
    applyNow(false);
  };

  /* ── The duplicate-cluster sheet ─────────────────────────────────────
     On a wide window the DUP badge does two things: hover states what it
     duplicates, click drills into the cluster. Touch has no hover and DESIGN.md
     13.1 forbids information reachable only that way, so at this width the two
     collapse into one sheet - it names the cluster, lists the other copies, says
     which one is KEPT and why, and carries the drill-in as a button. */
  function openDupSheet(id) {
    const row = channelOnPage(id);
    if (!row || !row.dup) return;
    const d = row.dup;
    const others = d.others || [];
    const more = d.others_hidden
      ? `<div class="prow"><span class="pv text-faint">+ ${nf(d.others_hidden)} more</span></div>` : '';
    const sheet = openSheet({
      kind: 'dup',
      title: 'Duplicated stream URL',
      body: `<div class="sh-note" style="padding-top:2px">${nf(d.count - 1)} other channel${
          d.count === 2 ? '' : 's'} point at the identical stream URL. They are duplicates because
          they share a URL, not because they share a name.</div>
        ${others.map((o) => `<div class="prow"><span class="pv">${esc(o.name)}</span>
          <span class="hint">${esc(o.category || 'no category')}</span></div>`).join('')}${more}
        <div class="psep"></div>
        <div class="sh-note" style="padding-top:0">The one kept is ${esc(d.kept_reason)}.
          The rule is: in your guide first, then in a channel group, then best health, then
          lowest channel id.</div>
        <div style="padding:4px 0 6px"><button class="btn btn-primary" data-dupdrill="${row.id}"
          style="width:100%;justify-content:center">Show just these ${nf(d.count)} channels</button></div>`,
    });
    sheet.addEventListener('click', (e) => {
      const drill = e.target.closest('[data-dupdrill]');
      if (!drill) return;
      closeSheet();
      dupDrillIn(Number(drill.dataset.dupdrill));
    });
  }

  /* ── Saved searches ──────────────────────────────────────────────────
     Server-side through the generic /api/user-prefs row (DESIGN.md 3.11), the
     same place the column setup lives, so a saved search follows the user across
     browsers. One row holds the whole list; the route renders it into the first
     paint (CFG.savedSearches), which is why nothing is fetched here.

     A record is { name, params, is_default }, and `params` is the query string
     above - so a saved search carries the text, the scope fields, the match mode,
     THE STANDING OPTIONS and every filter, not just the chips. The standing
     options are the reason it cannot be "just the chips": one that silently kept
     whatever the hiders happen to be set to now would give the same saved search
     two different result counts on two different days
     (DESIGN-channel-search.md §5). It is also why `params` and not a bespoke
     object - `set default` is read by the ROUTE, which redirects a bare /channels
     to that query string, and a query string is the one thing the server can act
     on without a second parser. */
  let savedSearches = Array.isArray(CFG.savedSearches) ? CFG.savedSearches : [];
  let loadedSaved = null;                    // name of the last one loaded or saved
  let sfName = '';                           // the name box, kept out of the DOM
  // Set only when a persist POST actually failed, so the popover/sheet can say so
  // durably (CLAUDE.md Product Principle 1) instead of relying on a 5s toast that's
  // easy to miss. Cleared on the next successful persist or when the panel reopens.
  let sfError = '';

  /* What makes two searches "the same one". Paging through a saved search, or
     asking for different counts, must not make it read as edited - and the action
     context is an errand rather than part of the search. */
  const IDENTITY_SKIP = ['page', 'per_page', 'facets', 'add_to_group', 'replace_rec'];
  function paramsKey(params) {
    const p = new URLSearchParams(params || '');
    IDENTITY_SKIP.forEach((k) => p.delete(k));
    return Array.from(p.entries()).map(([k, v]) => `${k}=${v}`).sort().join('&');
  }
  const savedByName = (name) => savedSearches.find((s) => s.name === name) || null;

  /* DERIVED, never a flag something has to remember to set. The mockup carried a
     savedDirty boolean and a markDirty() call in every handler that changed
     anything, which is one missed call site away from a saved search quietly
     reporting itself unedited. Comparing the two states cannot miss one. */
  function savedStatus() {
    const key = paramsKey(snapshot());
    const loaded = loadedSaved && savedByName(loadedSaved);
    // The one just loaded or saved wins over an identical twin saved under another
    // name - otherwise saving a copy of a search would show the copy's name.
    if (loaded && paramsKey(loaded.params) === key) return { name: loaded.name, dirty: false };
    const match = savedSearches.find((s) => paramsKey(s.params) === key);
    if (match) return { name: match.name, dirty: false };
    return loaded ? { name: loaded.name, dirty: true } : { name: '', dirty: false };
  }

  function renderSavedState() {
    const st = savedStatus();
    $('#sf-current').textContent = st.name;
    $('#sf-dirty').style.display = st.dirty ? '' : 'none';
  }

  /* Callers already paint their change optimistically (name next to the button, the
     popover row, the toast) before this resolves - so on failure `rollback` is what
     undoes that local mutation, and every surface gets repainted for real rather than
     trusting the toast alone to be seen (CLAUDE.md Product Principle 1: an optimistic
     update that outruns confirmation must visibly roll back on failure, not just log
     it somewhere easy to miss). */
  async function persistSaved(rollback) {
    try {
      await jsonFetch(CFG.prefUrlBase + CFG.savedPrefKey, {
        method: 'POST', body: JSON.stringify({ value: savedSearches }),
      });
      if (sfError) { sfError = ''; renderSavedPop(); renderOpenSheet(); }
    } catch (e) {
      rollback();
      sfError = 'Could not reach the server - this change was not saved, and has been undone here too. Try again.';
      renderSavedPop();
      renderOpenSheet();
      renderSavedState();
      renderChipRow();
      showToast(sfError, { type: 'error' });
    }
  }

  // A saved search's own summary, so the list says what each one does rather than
  // only what it was called. Read from the params, so it cannot describe something
  // the search does not actually carry.
  function describeSaved(params) {
    const p = new URLSearchParams(params || '');
    const bits = [];
    const q = p.get('q');
    if (q) bits.push(`"${q}"`);
    let n = 0;
    p.forEach((v, k) => { if (k.startsWith('f.') || k.startsWith('x.')) n += 1; });
    if (n) bits.push(`${n} filter value${n === 1 ? '' : 's'}`);
    // How many options DIFFER from their default, not how many are ticked. Counting ticks
    // described the set while every option meant "hide"; with six of them meaning "show",
    // an empty set is the most restrictive search there is rather than the loosest, and
    // "no standing options" would have named it exactly backwards (dev/changelog/778).
    const standing = new Set(p.getAll('standing').filter(Boolean));
    const defaults = defaultStandingFor();
    if (p.getAll('standing').length) {
      const moved = standingFor().filter((s) => standing.has(s.key) !== defaults.includes(s.key));
      if (moved.length) {
        bits.push(`${moved.length} standing option${moved.length === 1 ? '' : 's'} changed`);
      }
    }
    return bits.length ? bits.join(' + ') : 'everything';
  }

  function renderSavedPop() {
    const body = $('#sf-pop-body');
    if (!body) return;
    // Same reason renderFilterPop() and renderRail() do it: the panel is rebuilt
    // whole, and the caret in the name box has to survive that.
    const active = document.activeElement;
    const caret = active && active.dataset && active.dataset.sfname !== undefined
      ? active.selectionStart : null;

    body.innerHTML = '<h5>Saved searches</h5>' + savedBodyHtml();

    if (caret !== null) {
      const box = $('#sf-newname');
      if (box) {
        box.focus();
        try { box.setSelectionRange(caret, caret); } catch (e) { /* no selection API here */ }
      }
    }
  }

  /* The list, the name box and the note, shared by the desktop panel and the phone
     sheet - two arrangements of one control, so a saved search cannot behave one
     way in a popover and another in a sheet. */
  function savedBodyHtml() {
    const rows = savedSearches.map((s, i) => `<div class="prow" data-load="${i}" style="cursor:pointer"
        data-tip="${tipAttr(`${s.name}\n${describeSaved(s.params)}\n\nClick to load it.`)}">
        <span class="pv">${esc(s.name)}${s.is_default ? ' <span class="badge b-auto">default</span>' : ''}</span>
        <button class="btn btn-sm btn-ghost" data-default="${i}" data-tip="${tipAttr(s.is_default
          ? 'Stop opening this page with this search. A bare /channels then shows everything again.'
          : 'Open this page with this search already applied. The address bar shows it too, because the server redirects rather than applying it silently.')}"
          >${s.is_default ? 'clear default' : 'set default'}</button>
        <button class="btn btn-sm btn-ghost" data-del="${i}" data-tip="Delete this saved search.">&times;</button>
      </div>`).join('');
    return (rows || '<div class="pnote" style="margin-top:0">Nothing saved yet. Build a search, then name it here.</div>')
      + `<div class="psep"></div>
         <div class="pfoot"><input class="pop-search" data-sfname="1" id="sf-newname"
           value="${esc(sfName)}" placeholder="Name this search&#8230;" autocomplete="off">
           <button class="btn btn-sm btn-primary" data-save="1">Save</button></div>`
      + (sfError ? `<div class="pnote" style="color:var(--bad)">${esc(sfError)}</div>` : '')
      + `<div class="pnote">Saved on the server, so they follow you across browsers. Each one
           carries the text, the search scope, the match mode, the standing options and every
           filter - not just the chips.</div>`;
  }

  /* The phone's Saved surface. It is a chip plus a sheet rather than a dropdown
     under the search box (which is where DESIGN.md 13.4 anchors the guide's saved
     searches): that slot is taken here by the suggestion menu, which has to be
     there while you type. A deliberate divergence, stated rather than left to
     look like an oversight. */
  function openSavedSheet() {
    sfName = loadedSaved || '';
    sfError = '';
    const redraw = () => {
      const body = sheetBody();
      // The name box is inside what is about to be rewritten, so the caret has to
      // survive it - the same reason the desktop panel restores it.
      const active = document.activeElement;
      const caret = active && active.dataset && active.dataset.sfname !== undefined
        ? active.selectionStart : null;
      body.innerHTML = savedBodyHtml();
      if (caret !== null) {
        const box = body.querySelector('[data-sfname]');
        if (box) {
          box.focus();
          try { box.setSelectionRange(caret, caret); } catch (e) { /* no selection API here */ }
        }
      }
    };
    const sheet = openSheet({
      kind: 'saved', title: 'Saved searches', body: savedBodyHtml(), redraw,
    });
    sheet.addEventListener('click', (e) => savedAction(e, redraw));
    sheet.addEventListener('input', (e) => {
      if (e.target.closest('[data-sfname]')) sfName = e.target.value;
    });
    sheet.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.closest('[data-sfname]')) {
        e.preventDefault();
        saveCurrentSearch();
      }
    });
  }

  function saveCurrentSearch() {
    const name = sfName.trim();
    if (!name) { showToast('Give the search a name first.', { type: 'error' }); return; }
    const existing = savedByName(name);
    const prevParams = existing ? existing.params : null;
    const prevLoadedSaved = loadedSaved;
    // Saving over the same name is how the "edited" badge is answered, so it
    // overwrites - but it says which of the two it did, because a silent
    // overwrite of somebody else's search is the failure mode here.
    if (existing) existing.params = snapshot();
    else savedSearches.push({ name, params: snapshot(), is_default: false });
    loadedSaved = name;
    persistSaved(() => {
      // Undo exactly what was just done above, so a failed persist can't leave the
      // in-memory list claiming something the server never actually stored.
      if (existing) existing.params = prevParams;
      else savedSearches = savedSearches.filter((s) => s.name !== name);
      loadedSaved = prevLoadedSaved;
    });
    // Both arrangements, unconditionally: only one of them is on screen, and the
    // other one is cheap to repaint into a hidden element.
    renderSavedPop();
    renderOpenSheet();
    renderSavedState();
    renderChipRow();
    showToast(existing ? `Updated "${name}".` : `Saved "${name}".`);
  }

  /* ── Dimensions: the values each facet offers ────────────────────────
     Every list here comes out of the catalog, which generates it from the
     engine's own registries - the page must not re-type one. The only local
     part is `create`, which is a page action (where to go to make one), not
     something the engine knows about. */
  /* ── `when`: three fixed values and two you fill in ───────────────────
     TIME IS A FILTER DIMENSION, not two loose datetime inputs (mockup 25, P5) - so it
     chips, negates, saves, and shows up in the URL like every other filter.

     THERE IS NO DEFAULT WINDOW. A hidden default is silent narrowing, and this
     project's founding principle is that nothing is silent. With no `when` filter the
     grain lists every future showing ordered by start time, and the count line says how
     many.

     The engine serves the three fixed values and deliberately does NOT serve the two
     parametrized ones: there are infinitely many `next:` and `custom:` windows, the page
     builds them from its own controls, and the engine parses whatever arrives
     (DESIGN-channel-search.md 9.2). So these two are the one place the page spells a
     filter value itself, and the spelling is an API - see app/channel_search.py's
     WHEN_NEXT_PREFIX / WHEN_CUSTOM_PREFIX.

     THE SEPARATOR INSIDE `custom` IS `..`, NOT a third colon: a local wall-clock time
     contains one (2026-08-01T19:00), so custom:<from>:<to> could not be split back apart. */
  const WHEN_KEY = 'when';
  const WHEN_NEXT_PREFIX = 'next:';
  const WHEN_CUSTOM_PREFIX = 'custom:';
  const WHEN_RANGE_SEP = '..';
  const WHEN_UNITS = ['minutes', 'hours', 'days'];
  // The state BEHIND the two values, held outside state.filters because a filter value
  // is one string in a URL and these are the two or three controls that spell it.
  const whenRel = { n: '', unit: 'hours' };
  const whenCustom = { from: '', to: '' };

  const relSet = () => Number(whenRel.n) > 0;
  const relValue = () => (relSet() ? `${WHEN_NEXT_PREFIX}${Number(whenRel.n)}:${whenRel.unit}` : '');
  const customSet = () => !!(whenCustom.from || whenCustom.to);
  const customValue = () =>
    (customSet() ? `${WHEN_CUSTOM_PREFIX}${whenCustom.from}${WHEN_RANGE_SEP}${whenCustom.to}` : '');

  /* ── `duration`: one range you fill in, wired the same way as `when`'s custom range ────
     A duration bound is just a number - there is no `next:`-equivalent relative half, so
     this is one parametrized value, not two. The wire format is always MINUTES
     (`dur:<min>..<max>`, matching EPGEntry.duration_minutes); the unit selector is display
     only, so typing "3 hours" and reloading the page still shows "3 hours", not "180". */
  const DURATION_KEY = 'duration';
  const DUR_PREFIX = 'dur:';
  const DUR_UNITS = ['minutes', 'hours'];
  const durationRange = { min: '', max: '', unit: 'hours' };

  const durToMinutes = (n, unit) => (unit === 'hours' ? Math.round(Number(n) * 60) : Math.round(Number(n)));
  const durationSet = () => !!(durationRange.min || durationRange.max);
  const durationValue = () => {
    if (!durationSet()) return '';
    const lo = durationRange.min ? durToMinutes(durationRange.min, durationRange.unit) : '';
    const hi = durationRange.max ? durToMinutes(durationRange.max, durationRange.unit) : '';
    return `${DUR_PREFIX}${lo}${WHEN_RANGE_SEP}${hi}`;
  };
  const durationLabel = () => {
    const lo = Number(durationRange.min) || 0;
    const hi = Number(durationRange.max) || 0;
    const u = (n) => (n === 1 ? durationRange.unit.slice(0, -1) : durationRange.unit);
    if (lo && hi) return `${lo} to ${hi} ${u(hi)}`;
    if (lo) return `Longer than ${lo} ${u(lo)}`;
    if (hi) return `Shorter than ${hi} ${u(hi)}`;
    return 'Program length';
  };

  /* Read the controls back out of whatever the URL carried, so a reload - or loading a
     saved search - restores the boxes and not just the chip. Without this the chip would
     say "Next 3 hours" over two empty controls, which is a filter you cannot edit. */
  function hydrateWhenControls() {
    const f = findF(WHEN_KEY);
    if (!f) return;
    f.values.concat(f.ex).forEach((v) => {
      if (v.startsWith(WHEN_NEXT_PREFIX)) {
        const [n, unit] = v.slice(WHEN_NEXT_PREFIX.length).split(':');
        if (WHEN_UNITS.includes(unit) && Number(n) > 0) { whenRel.n = n; whenRel.unit = unit; }
      } else if (v.startsWith(WHEN_CUSTOM_PREFIX)) {
        const body = v.slice(WHEN_CUSTOM_PREFIX.length);
        if (body.includes(WHEN_RANGE_SEP)) {
          const i = body.indexOf(WHEN_RANGE_SEP);
          whenCustom.from = body.slice(0, i);
          whenCustom.to = body.slice(i + WHEN_RANGE_SEP.length);
        }
      }
    });
  }

  /* Same reason as hydrateWhenControls: the wire value is always minutes, so this also picks
     the DISPLAY unit back - hours if both bounds present divide evenly, minutes otherwise -
     so a reload of "3 hours" doesn't turn into "180 minutes". */
  function hydrateDurationControls() {
    const f = findF(DURATION_KEY);
    if (!f) return;
    f.values.concat(f.ex).forEach((v) => {
      if (!v.startsWith(DUR_PREFIX)) return;
      const body = v.slice(DUR_PREFIX.length);
      if (!body.includes(WHEN_RANGE_SEP)) return;
      const i = body.indexOf(WHEN_RANGE_SEP);
      const rawMin = body.slice(0, i);
      const rawMax = body.slice(i + WHEN_RANGE_SEP.length);
      const minN = rawMin === '' ? null : Number(rawMin);
      const maxN = rawMax === '' ? null : Number(rawMax);
      if (minN === null && maxN === null) return;
      const wholeHours = (n) => n === null || (Number.isFinite(n) && n > 0 && n % 60 === 0);
      const unit = wholeHours(minN) && wholeHours(maxN) ? 'hours' : 'minutes';
      durationRange.unit = unit;
      durationRange.min = minN === null ? '' : String(unit === 'hours' ? minN / 60 : minN);
      durationRange.max = maxN === null ? '' : String(unit === 'hours' ? maxN / 60 : maxN);
    });
  }

  /* The value reads as the window it currently describes, so the chip says "Next 3 hours"
     and not "Next ..." - a chip has to be readable without opening the facet that made it.
     Singular when n is 1, because "Next 1 days" makes a page look unfinished. */
  const relLabel = () => {
    if (!relSet()) return 'Next ...';
    const n = Number(whenRel.n);
    return `Next ${n} ${n === 1 ? whenRel.unit.slice(0, -1) : whenRel.unit}`;
  };
  const customLabel = () => (customSet()
    ? `${whenCustom.from || 'any time'} to ${whenCustom.to || 'any time'}` : 'Custom range');

  function dimValues(key) {
    if (!CAT) return [];
    switch (key) {
      // The three fixed values, plus each parametrized one only once its control holds
      // something. "Everything" is not a useful answer for a value whose whole job is to
      // narrow, and an unfilled control is not a window.
      case WHEN_KEY: {
        const out = (CAT.when_values || []).map((w) => w.value);
        if (relSet()) out.push(relValue());
        if (customSet()) out.push(customValue());
        return out;
      }
      // No closed vocabulary at all (unlike `when`'s three fixed values) - the range control
      // is the only source of a value.
      case DURATION_KEY: return durationSet() ? [durationValue()] : [];
      case 'tag':    return CAT.tags.map((t) => t.name);
      case 'acct':   return CAT.accounts.map((a) => String(a.id));
      case 'health': return CAT.health_values.map((h) => h.value);
      case 'group':  return [CAT.group_any].concat(CAT.groups.map((g) => g.name));
      case 'cat':    return CAT.categories;
      case 'other':  return CAT.other_values.map((o) => o.value);
      default:       return [];       // `chan` is reachable from the suggestion menu only
    }
  }

  function dimLabel(key, value) {
    if (!CAT) return value;
    const byValue = (list, k = 'value', v = 'label') =>
      (list.find((x) => String(x[k]) === String(value)) || {})[v];
    switch (key) {
      case 'acct':   return (CAT.accounts.find((a) => String(a.id) === value) || {}).name || value;
      case 'health': return byValue(CAT.health_values) || value;
      case 'other':  return byValue(CAT.other_values) || value;
      case 'group':  return value === CAT.group_any ? 'Any group' : value;
      case WHEN_KEY: {
        if (String(value).startsWith(WHEN_NEXT_PREFIX)) return relLabel();
        if (String(value).startsWith(WHEN_CUSTOM_PREFIX)) return customLabel();
        return byValue(CAT.when_values || []) || value;
      }
      case DURATION_KEY:
        return String(value).startsWith(DUR_PREFIX) ? durationLabel() : value;
      case 'chan': {
        const ch = pageChannels().find((c) => String(c.id) === String(value));
        return ch ? ch.name : `Channel #${value}`;
      }
      default:       return value;
    }
  }

  // Per-value tooltip, distinct from a dimension's own `dim.help` (the (i) icon on the
  // facet header): only 'other'/OTHER_NEW carries one today, because its label alone
  // does not say how long a channel keeps showing as new.
  function dimTip(key, value) {
    if (!CAT || key !== 'other') return null;
    const found = (CAT.other_values || []).find((o) => o.value === value);
    return (found && found.tip) || null;
  }

  // Where to go to make one of these. A page action, so it is not in the catalog.
  const DIM_CREATE = {
    // Plain navigation to the tag list with its create modal open (?new=1). Creating a
    // tag leaves you on that list rather than back here - there is no return-address
    // parameter, so linking one would promise a return that does not happen.
    tag: { label: '+ Create tag', run: () => { location.href = CFG.newTagUrl; } },  // nav-ok: menu item
    // Groups are made FROM channels - there is no blank-group screen, and
    // create-group-modal.js clones an existing health check rather than starting
    // empty. So this goes to the Groups page. B5 should point it at the same
    // "Group selected" modal the selection bar uses once a selection exists,
    // which is the flow that actually creates one from here.
    group: { label: '+ Create group', run: () => { location.href = CFG.groupsUrl; } },  // nav-ok: menu item
  };

  /* ── Filter state helpers ────────────────────────────────────────────
     Values inside one dimension OR together; dimensions AND together. The
     shipped recordings list ANDs same-field values, so picking two of anything
     there matches nothing - that is the defect this model fixes rather than
     copies. The engine enforces the same split server-side. */
  const findF = (key) => state.filters.find((f) => f.key === key);
  function getF(key) {
    let f = findF(key);
    if (!f) { f = { key, values: [], ex: [] }; state.filters.push(f); }
    return f;
  }
  const tidyFilters = () => {
    state.filters = state.filters.filter((f) => f.values.length || f.ex.length);
  };
  const nSet = () => state.filters.reduce((a, f) => a + f.values.length + f.ex.length, 0);

  function valueState(key, value) {
    const f = findF(key);
    if (!f) return 0;
    if (f.values.includes(value)) return 1;
    if (f.ex.includes(value)) return -1;
    return 0;
  }
  function toggleValue(key, value, asEx) {
    const f = getF(key);
    const list = asEx ? f.ex : f.values;
    const other = asEx ? f.values : f.ex;
    const i = list.indexOf(value);
    if (i >= 0) list.splice(i, 1);
    else {
      list.push(value);
      const j = other.indexOf(value);
      if (j >= 0) other.splice(j, 1);
    }
    tidyFilters();
    applyNow();
  }

  /* ── The rail ────────────────────────────────────────────────────── */
  const RAIL_SHOW = 6;                       // values before the find box and Show all
  // Derived from the catalog rather than typed out, so a facet added later
  // starts expanded too instead of silently reintroducing a mix.
  let railOpen = null;
  const railShowAll = new Set();
  const railQ = {};
  let railCollapsed = false;

  // A count of null is "this dimension was not counted", which the rail must
  // render as such - it is NOT a zero (DESIGN-channel-search.md 6).
  function countOf(key, value) {
    if (!facetsCounted || !facetsCounted.includes(key)) return null;
    return (facets[key] || {})[value] || 0;
  }

  // `attr` is which data-* the box answers to: the rail's boxes are data-railq (one per
  // facet), the + Filter popover's is data-popq. Two names rather than one, because
  // renderRail() restores focus by looking its own boxes up by that attribute and must not
  // find the popover's.
  function findBoxHtml(cls, value, placeholder, key, attr = 'railq') {
    return `<span class="findwrap"><input class="${cls}" data-${esc(attr)}="${esc(key)}"
      value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">
      ${value ? `<button class="fclr" data-findclr="${esc(key)}" data-tip="Clear this">&#10005;</button>` : ''}</span>`;
  }

  function valueRowHtml(key, value, label, count, tip) {
    const st = valueState(key, value);
    const kv = `data-key="${esc(key)}" data-value="${esc(value)}"`;
    const countHtml = count === null
      ? '<span class="pcount uncounted" data-tip="Not counted for this search.">--</span>'
      : `<span class="pcount${count ? '' : ' zero'}">${nf(count)}</span>`;
    return `<div class="prow${st ? ' sel-on' : ''}" ${kv}>
      <span class="pv"${tip ? ` data-tip="${tipAttr(tip)}"` : ''}>${esc(label)}</span>${countHtml}
      <span class="tri"><button ${kv} data-dir="inc" class="${st === 1 ? 'on-inc' : ''}"
        data-tip="${tipAttr(`Include ${label}`)}">+</button><button ${kv} data-dir="exc" class="${st === -1 ? 'on-exc' : ''}"
        data-tip="${tipAttr(`Exclude ${label}`)}">&minus;</button></span></div>`;
  }

  function facetHtml(dim) {
    const f = findF(dim.key);
    const n = f ? f.values.length + f.ex.length : 0;
    const hasNeg = !!(f && f.ex.length);
    const values = dimValues(dim.key);
    const hasBox = values.length > RAIL_SHOW;
    const q = (hasBox ? (railQ[dim.key] || '') : '').trim().toLowerCase();

    // No reordering, ever: a value must stay where it was when you click it.
    // Selection reads from .sel-on and the count badge instead.
    let scored = values.map((v) => ({ v, label: String(dimLabel(dim.key, v)),
                                      c: countOf(dim.key, v), st: valueState(dim.key, v),
                                      tip: dimTip(dim.key, v) }));
    if (q) scored = scored.filter((x) => x.label.toLowerCase().includes(q));
    const open = railOpen.has(dim.key) || n > 0 || (!!q && !!scored.length);
    // A value you turned on must never vanish because the list is collapsed, and
    // must never MOVE either. Opening the whole facet is the only spelling that
    // does both.
    const hiddenSel = scored.slice(RAIL_SHOW).some((x) => x.st !== 0);
    const all = railShowAll.has(dim.key) || !!q || hiddenSel;
    const shown = all ? scored : scored.slice(0, RAIL_SHOW);
    // A "show all" under a set of find-box results would be asking twice for the
    // same thing.
    const moreBtn = !q && (scored.length > shown.length || all)
      ? `<button class="fac-more" data-more="${dim.key}">${all ? 'Show less' : `Show all ${nf(scored.length)}`}</button>` : '';
    const box = hasBox
      ? findBoxHtml('rsearch', railQ[dim.key] || '', `Find in ${dim.label.toLowerCase()}...`, dim.key) : '';
    const what = dim.help ? ` <span class="fwhat" data-tip="${tipAttr(dim.help)}">&#9432;</span>` : '';
    // The create button is inside the header's own click target, so its branch
    // has to be tested BEFORE the header's, or creating would just collapse the
    // facet. It is deliberately reachable with the facet collapsed.
    const make = DIM_CREATE[dim.key]
      ? ` <button class="fac-make" data-make="${dim.key}">${esc(DIM_CREATE[dim.key].label)}</button>` : '';

    return `<div class="fac${open ? ' open' : ''}" data-fac="${dim.key}">
      <div class="fac-h" data-fach="${dim.key}"><span class="caret">&#9654;</span><span class="fac-name">${esc(dim.label)}</span>${what}${make}
        ${n ? `<span class="nsel${hasNeg ? ' hasneg' : ''}">${n}</span>` : ''}</div>
      <div class="fac-b">
        ${box}
        ${shown.map((x) => valueRowHtml(dim.key, x.v, x.label, x.c, x.tip)).join('')}
        ${shown.length ? '' : '<div class="fac-none">no value matches that</div>'}
        ${dim.key === WHEN_KEY && !q ? whenControlsHtml() : ''}
        ${dim.key === DURATION_KEY && !q ? durationControlsHtml() : ''}
        ${moreBtn}
      </div></div>`;
  }

  /* ── The two `when` values you fill in ───────────────────────────────
     EACH CONTROL SITS UNDER THE VALUE THAT OWNS IT (mockup 25 round 5: "Under each
     value. Final answer."), and the rule that goes with it: until a control holds a
     value, its row counts n/a and can be neither included nor excluded. That is why the
     tri-state buttons are only drawn once the value exists - dimValues() leaves it out
     of the list entirely until then, so this block draws the row's controls and the
     generic valueRowHtml() draws the row itself the moment there is something to filter on.

     A number and a unit answers every window in between, and it is the one shape that
     does not have to be argued about: round 2's fixed list ("next 3 hours",
     "tonight", "next 7 days") was cut because a fixed list is arbitrary and "tonight" needs a rule
     about when evening starts, and any such rule is somebody's wrong. */
  function whenControlsHtml() {
    return `<div class="when-fill${relSet() ? ' on' : ''}">
        <span class="wf-l">${esc(relSet() ? relLabel() : 'Next ...')}</span>
        <span class="wf-c">
          <input class="wf-n" type="number" min="1" step="1" inputmode="numeric"
            data-whenrel="n" value="${esc(whenRel.n)}" placeholder="n"
            aria-label="How many" data-tip="How many, counting from now. Empty means this value is off.">
          <select class="wf-u" data-whenrel="unit" aria-label="Unit">
            ${WHEN_UNITS.map((u) => `<option value="${u}"${whenRel.unit === u ? ' selected' : ''}>${u}</option>`).join('')}
          </select>
        </span>
      </div>
      <div class="when-fill${customSet() ? ' on' : ''}">
        <span class="wf-l">${esc(customSet() ? customLabel() : 'Custom range')}</span>
        <span class="wf-c wf-range">
          <input type="datetime-local" data-whencustom="from" value="${esc(whenCustom.from)}"
            aria-label="From" data-tip="Showings that START at or after this. Leave it empty for no lower bound.">
          <input type="datetime-local" data-whencustom="to" value="${esc(whenCustom.to)}"
            aria-label="To" data-tip="Showings that START at or before this. Leave it empty for no upper bound.">
        </span>
      </div>
      <div class="fac-note">Both are shown in ${esc(displayTz())}, the display timezone. One bound on its
        own is a real window.</div>`;
  }

  /* One control, not two - a duration bound has no `when`-style relative half, it is just a
     number. Reuses the `when-fill`/`wf-*` classes rather than a page-local rule of its own
     (CLAUDE.md's DRY canonical-home discipline: this is the same shape of control, not a
     different one). */
  function durationControlsHtml() {
    return `<div class="when-fill${durationSet() ? ' on' : ''}">
        <span class="wf-l">${esc(durationSet() ? durationLabel() : 'Length...')}</span>
        <span class="wf-c wf-range">
          <input class="wf-n" type="number" min="0" step="1" inputmode="numeric"
            data-durrange="min" value="${esc(durationRange.min)}" placeholder="min"
            aria-label="At least" data-tip="Showings running at least this long. Leave it empty for no lower bound.">
          <input class="wf-n" type="number" min="0" step="1" inputmode="numeric"
            data-durrange="max" value="${esc(durationRange.max)}" placeholder="max"
            aria-label="At most" data-tip="Showings running at most this long. Leave it empty for no upper bound.">
          <select class="wf-u" data-durrange="unit" aria-label="Unit">
            ${DUR_UNITS.map((u) => `<option value="${u}"${durationRange.unit === u ? ' selected' : ''}>${u}</option>`).join('')}
          </select>
        </span>
      </div>
      <div class="fac-note">Either bound alone is a real filter.</div>`;
  }

  /* Editing a control REPLACES the value it owns rather than adding a second one: there
     is one "Next ..." window and one custom range, so typing a new number must not leave
     the old window still filtering. Whether that value is currently an include or an
     exclude is preserved, because retyping a number is not a decision to flip it. */
  function setWhenValue(prefix, next) {
    const f = findF(WHEN_KEY);
    let wasEx = false;
    if (f) {
      wasEx = f.ex.some((v) => v.startsWith(prefix));
      f.values = f.values.filter((v) => !v.startsWith(prefix));
      f.ex = f.ex.filter((v) => !v.startsWith(prefix));
    }
    if (next) {
      const target = getF(WHEN_KEY);
      (wasEx ? target.ex : target.values).push(next);
    }
    tidyFilters();
    applyNow();
  }

  /* Only one parametrized value for `duration` (no `next:`-equivalent), so unlike
     setWhenValue there is no prefix to disambiguate - any existing `dur:` value is simply
     replaced. */
  function setDurationValue(next) {
    const f = findF(DURATION_KEY);
    let wasEx = false;
    if (f) {
      wasEx = f.ex.some((v) => v.startsWith(DUR_PREFIX));
      f.values = f.values.filter((v) => !v.startsWith(DUR_PREFIX));
      f.ex = f.ex.filter((v) => !v.startsWith(DUR_PREFIX));
    }
    if (next) {
      const target = getF(DURATION_KEY);
      (wasEx ? target.ex : target.values).push(next);
    }
    tidyFilters();
    applyNow();
  }

  /* Delegated on the document, because the same controls are drawn in the rail at one
     width and inside the Filters sheet at the other - one handler, both hosts. Debounced
     on the number box only: a datetime-local fires `change` when it is complete, but a
     number fires `input` per keystroke and "1" on the way to "180" is a real window that
     would otherwise run a search of its own. */
  let whenTimer = null;
  let durationTimer = null;
  document.addEventListener('input', (e) => {
    const rel = e.target.closest('[data-whenrel]');
    if (rel) {
      whenRel[rel.dataset.whenrel] = rel.value;
      clearTimeout(whenTimer);
      whenTimer = setTimeout(() => setWhenValue(WHEN_NEXT_PREFIX, relValue()), DEBOUNCE_MS);
      return;
    }
    const cust = e.target.closest('[data-whencustom]');
    if (cust) {
      whenCustom[cust.dataset.whencustom] = cust.value;
      clearTimeout(whenTimer);
      whenTimer = setTimeout(() => setWhenValue(WHEN_CUSTOM_PREFIX, customValue()), DEBOUNCE_MS);
      return;
    }
    const dur = e.target.closest('[data-durrange]');
    if (dur) {
      durationRange[dur.dataset.durrange] = dur.value;
      clearTimeout(durationTimer);
      durationTimer = setTimeout(() => setDurationValue(durationValue()), DEBOUNCE_MS);
    }
  });
  document.addEventListener('change', (e) => {
    const rel = e.target.closest('select[data-whenrel]');
    if (rel) {
      whenRel[rel.dataset.whenrel] = rel.value;
      clearTimeout(whenTimer);
      setWhenValue(WHEN_NEXT_PREFIX, relValue());
      return;
    }
    const durUnit = e.target.closest('select[data-durrange]');
    if (durUnit) {
      durationRange[durUnit.dataset.durrange] = durUnit.value;
      clearTimeout(durationTimer);
      setDurationValue(durationValue());
    }
  });

  /* The standing tier. It is the rail's FOOTER, below the facets: what you touch
     on most searches leads, what you set once comes last. Written by renderRail()
     like every other card - injecting it from outside would not survive, because
     renderRail() rewrites the whole element. */
  /* Both surfaces write through these, so "turning a standing option off" is one
     rule rather than one per arrangement. */
  function toggleStanding(key) {
    if (state.standing.has(key)) state.standing.delete(key); else state.standing.add(key);
    applyNow();
  }
  function resetStanding() {
    state.standing = new Set(defaultStandingFor());
    applyNow();
  }

  const standingOpts = () => standingFor();
  /* The badge counts what is being REMOVED, not what is ticked. Those were the same number
     while every label said "Hide ..."; now that six of them say "Show ...", counting ticks
     would report how much you are letting through, which is not what a count beside a list
     of exclusions means (dev/changelog/778). */
  const standingHidingCount = () => standingOpts().filter(standingApplied).length;
  const standingIsDefault = () => standingOpts().every((s) => state.standing.has(s.key) === s.default);
  const standingResetHtml = () => (standingIsDefault() ? ''
    : '<button class="rclr" id="st-reset" data-tip="Put every standing option back to its default.">reset</button>');

  /* The toggles themselves, shared by the rail's footer card and the Filters
     sheet's footer block - the two arrangements of one control, not two
     controls. */
  function standingTogglesHtml() {
    return standingOpts().map((s) => {
      // Lit means the option is ON as its own label reads it; the count beside it belongs to
      // the other state, because an option only has something to report while it is hiding.
      const on = state.standing.has(s.key);
      const hiding = standingApplied(s);
      const n = (last.standingHidden || {})[s.key] || 0;
      return `<button class="st-tog${on ? ' on' : ''}" data-standing="${s.key}" data-tip="${tipAttr(
        `${s.label}${hiding && n ? `\n\nLeaving ${nf(n)} ${grainUi().noun}${n === 1 ? '' : 's'} out of this search right now.` : ''}` +
        '\n\nThis is a STANDING option, not a filter - it stays set across searches, and it lives in the URL.')}">
        <span class="st-dot"></span><span>${esc(s.label)}</span>
        ${hiding && n ? `<span class="st-n">${nf(n)}</span>` : ''}</button>`;
    }).join('');
  }

  function standingCardHtml() {
    return `<div class="rcard st-card" id="standcard">
      <h4><span class="rt">Standing options</span>${standingResetHtml()}<span class="scnt">${standingHidingCount()}</span></h4>
      ${standingTogglesHtml()}
      <div class="scopewhy">These options keep their settings across searches.</div></div>`;
  }

  const railBarHtml = () => `<div class="railbar"><button class="rcollapse" id="rail-toggle"
      data-tip="Hide the whole filters sidebar and widen the table">
      <span class="rcx">&#171;</span><span class="rclabel">Collapse filters</span></button></div>`;
  // Collapsed, the way back is the Filters box ITSELF - a <button> wearing
  // .rcard, so every pixel of it is the hit target.
  const railStubHtml = () => `<button class="rcard railstub" id="rail-toggle"
      data-tip="Show the filters sidebar again">
      <span class="rcx">&#187;</span>
      <div class="rspine">${nSet() ? `<span class="rn">${nSet()}</span>` : ''}Filters</div></button>`;

  function renderRail() {
    if (!CAT) return;
    const el = $('#rail');
    /* NOT BUILT at phone width, rather than built and hidden. On a phone the rail
       and + Filter are the same thing, so the Filters sheet does both jobs and a
       populated-but-hidden rail would be a second, invisible copy of every one of
       those controls sitting in the accessibility tree
       (dev/mockups/22-channel-search-mobile.html decision 1). */
    if (isMobile()) { el.innerHTML = ''; return; }
    if (railOpen === null) railOpen = new Set(CAT.dimensions.map((d) => d.key));
    // The rail is rebuilt whole, and a search response can land while a find box
    // has the caret in it - so which box was focused is read off the DOM here
    // rather than passed in by whichever caller happened to trigger the render.
    // Without this a find box drops focus after one character.
    const active = document.activeElement;
    /* Which control had the caret, by the attribute that identifies it. The find boxes
       are one per facet (data-railq); the `when` fill-ins are one each and identify
       themselves by which half of their value they spell. All of them are inside the
       element about to be rewritten, so all of them need restoring - without this a
       number box drops focus after ONE digit and "180" is unreachable. */
    const focusSel = !active || !active.dataset ? null
      : (active.dataset.railq !== undefined ? `[data-railq="${active.dataset.railq}"]`
        : (active.dataset.whenrel !== undefined ? `[data-whenrel="${active.dataset.whenrel}"]`
          : (active.dataset.whencustom !== undefined
            ? `[data-whencustom="${active.dataset.whencustom}"]` : null)));
    /* -1 means "put it at the END", which is the only correct answer for the inputs whose
       caret cannot be read: Chrome raises InvalidStateError on `selectionStart` for
       `type=number` and `type=datetime-local`. Defaulting to 0 instead - which is what a
       bare try/catch does - restores the caret to the FRONT, so typing "36" lands the 6
       before the 3 and applies a 63-hour window. Measured in a real browser
       (dev/changelog/414); jsdom cannot see it, because jsdom implements selectionStart on
       a number input where Chrome does not. */
    let caret = -1;
    if (focusSel) { try { caret = active.selectionStart; } catch (e) { caret = -1; } }
    if (caret === null || caret === undefined) caret = -1;

    el.classList.toggle('collapsed', railCollapsed);
    if (railCollapsed) { el.innerHTML = railStubHtml(); return; }

    const cards = dimsFor().filter((d) => !d.hidden).map(facetHtml).join('');
    /* SAID ONCE, AT THE TOP OF THE RAIL, not only as a `--` in each of ~80 value rows. The
       per-value `--` says "this number is absent"; it cannot say why, and eighty of them
       read as a broken page rather than a deliberate answer (dev/changelog/676). Only when
       the request has actually settled - while one is in flight the spinner is the honest
       state, and a note left over from the previous answer is not. */
    const railNote = (!facetsPending && facetsDeclined)
      ? `<div class="rnote" data-tip="${tipAttr(
        'Counting every value would cost seconds for this search. The filters still work - '
        + 'only the counts beside them are missing.' + whyDeclined())}">&#9888; Counts not available for this search. The filters still work.</div>`
      : '';
    el.innerHTML = railBarHtml()
      + `<div class="rcard"><h4><span class="rt">Narrow it down</span>
          ${facetsPending ? `<span class="spinner rail-spin" data-tip="${tipAttr(
            'Counting how many of the matching ' + (isAirings() ? 'showings' : 'channels')
            + ' fall under every value on this rail. On a broad search that is millions of '
            + 'rows, so it can take a few seconds - the list and the filters work meanwhile.'
          )}"></span>` : ''}
          ${nSet() ? '<button class="rclr" id="rail-clr">clear</button>' : ''}</h4>${railNote}${cards}</div>`
      + standingCardHtml();

    if (focusSel) {
      const inp = $(`#rail ${focusSel}`);
      if (inp) {
        inp.focus();
        const at = caret < 0 ? String(inp.value || '').length : caret;
        try { inp.setSelectionRange(at, at); } catch (e) { /* no selection API on this input */ }
      }
    }
  }

  /* ── The Filters sheet: the rail's replacement at phone width ─────────
     ON A PHONE THE FACET RAIL AND THE + Filter POPOVER ARE THE SAME THING.
     Desktop affords both only because a 234px column beside the table is free
     real estate; there is none here, so one of the two survives - and it is the
     popover's shape: a list of dimensions, then that dimension's values, with a
     way back.

     THIS IS NOT A REVERSAL of the rail's "every facet starts expanded". That rule
     is about the rail's accordions, and the rail is precisely what does not exist
     at this width; six expanded facets inside an 86vh sheet is roughly 1250px of
     scrolling to reach the last one.

     Everything the rail carried survives: the three-state on every value, the
     per-value counts, the find box past six values, Show all, both + Create
     buttons, and the standing options as the footer below the facets (what you touch
     every search first, what you set once last).

     `railShowAll` and `railQ` are REUSED rather than shadowed by a second pair:
     the rail does not exist down here, and "which values are expanded" and "what
     was typed into this dimension's find box" are the same two facts either way. */
  let fDim = null;

  // last.total is null while fetchCounts() is still out (dev/changelog/598) - "0
  // matches" would be a wrong claim, not a pending one, so this reads "matches" with no
  // number rather than a fabricated zero.
  const filtersSheetMatches = () => (last.total === null ? 'matches' : `${nf(last.total)} matches`);
  const filtersSheetTitle = () =>
    (fDim ? `${dimName(fDim)} - ${filtersSheetMatches()}` : `Filters - ${filtersSheetMatches()}`);

  function filtersSheetBody() {
    if (!CAT) return '';
    if (!fDim) {
      const rows = dimsFor().filter((d) => !d.hidden).map((d) => {
        const f = findF(d.key);
        const n = f ? f.values.length + f.ex.length : 0;
        const hasNeg = !!(f && f.ex.length);
        return `<div class="prow drill${n ? ' sel-on' : ''}" data-fdim="${esc(d.key)}">
          <span class="pico">${dimIcon(d.key)}</span><span class="pv">${esc(d.label)}</span>
          ${n ? `<span class="mn${hasNeg ? ' hasneg' : ''}">${n}</span>` : ''}
          <span class="pchev">&#8250;</span></div>`;
      }).join('');
      return rows
        + `<div class="st-block">
             <h4><span>Standing options</span>${standingResetHtml()}<span class="scnt">${standingHidingCount()}</span></h4>
             ${standingTogglesHtml()}
             <div class="scopewhy">These options keep their settings across searches.</div>
           </div>
           <div class="sh-note">Values inside one filter are OR'd together. Different filters are
             AND'd. Each value is off, included or excluded, right where you are looking at it.</div>`;
    }

    const dim = dimByKey(fDim) || { key: fDim, label: fDim, help: '' };
    const values = dimValues(fDim);
    const q = (railQ[fDim] || '').trim().toLowerCase();
    // No reordering, same as the rail: a value must stay where it was when you
    // tapped it. Selection reads from .sel-on and the header count instead.
    let scored = values.map((v) => ({ v, label: String(dimLabel(fDim, v)),
                                      c: countOf(fDim, v), st: valueState(fDim, v) }));
    if (q) scored = scored.filter((x) => x.label.toLowerCase().includes(q));
    // A value you turned on must never be out of reach behind Show all.
    const hiddenSel = scored.slice(RAIL_SHOW).some((x) => x.st !== 0);
    const all = railShowAll.has(fDim) || !!q || hiddenSel;
    const shown = all ? scored : scored.slice(0, RAIL_SHOW);
    const moreBtn = !q && (scored.length > shown.length || all)
      ? `<button class="fac-more" data-more="${esc(fDim)}">${
        all ? 'Show less' : `Show all ${nf(scored.length)}`}</button>` : '';
    const box = values.length > RAIL_SHOW
      ? findBoxHtml('rsearch', railQ[fDim] || '', `Find in ${dim.label.toLowerCase()}...`, fDim) : '';
    const make = DIM_CREATE[fDim]
      ? `<div class="prow"><span class="pv text-faint" style="font-size:.8rem">Not listed?</span>
          <button class="fac-make" data-make="${esc(fDim)}">${esc(DIM_CREATE[fDim].label)}</button></div>` : '';

    return `<button class="sh-back" data-fback="1">&#8592; All filters</button>
      ${box}
      ${shown.map((x) => valueRowHtml(fDim, x.v, x.label, x.c)).join('')}
      ${shown.length ? '' : '<div class="fac-none">no value matches that</div>'}
      ${fDim === WHEN_KEY && !q ? whenControlsHtml() : ''}
      ${fDim === DURATION_KEY && !q ? durationControlsHtml() : ''}
      ${moreBtn}${make}
      ${dim.help ? `<div class="sh-note">${esc(dim.help)}</div>` : ''}`;
  }

  function renderFiltersSheet() {
    const body = sheetBody();
    if (!body) return;
    /* The find box is inside the element about to be rewritten, so without this it
       drops focus after one character - the same problem the rail has and the same
       fix, and read off the DOM for the same reason: a search response can land
       while the caret is in there, so the caller cannot be trusted to say. */
    const active = document.activeElement;
    const focused = active && active.dataset && active.dataset.railq !== undefined;
    const caret = focused ? active.selectionStart : 0;
    setSheetTitle(filtersSheetTitle());
    body.innerHTML = filtersSheetBody();
    if (focused) {
      const inp = body.querySelector('[data-railq]');
      if (inp) {
        inp.focus();
        try { inp.setSelectionRange(caret, caret); } catch (e) { /* no selection API here */ }
      }
    }
  }

  /* One sheet, three ways in: the Filters chip, the well's + Filter pill, and a
     chip's own value (which lands straight on that dimension). `dim` is which
     level to open at, so a tap on "Account" from the suggestion menu lands on
     accounts rather than on the list of dimensions. */
  function openFiltersSheet(dim) {
    fDim = dim || null;
    if (fDim) railQ[fDim] = '';
    const sheet = openSheet({
      kind: 'filters', title: filtersSheetTitle(), body: filtersSheetBody(),
      redraw: renderFiltersSheet,
    });
    sheet.addEventListener('click', (e) => {
      // [data-make] and the three-state pair sit inside the rows they belong to,
      // so both are tested BEFORE the row - the other order silently turns
      // creating, and excluding, into drilling.
      const make = e.target.closest('[data-make]');
      if (make) { DIM_CREATE[make.dataset.make].run(); return; }
      const tri = e.target.closest('.tri button[data-key]');
      if (tri) { toggleValue(tri.dataset.key, tri.dataset.value, tri.dataset.dir === 'exc'); return; }
      const more = e.target.closest('[data-more]');
      if (more) {
        const key = more.dataset.more;
        if (railShowAll.has(key)) railShowAll.delete(key); else railShowAll.add(key);
        renderFiltersSheet();
        return;
      }
      const clr = e.target.closest('[data-findclr]');
      if (clr) { railQ[clr.dataset.findclr] = ''; renderFiltersSheet(); return; }
      const drill = e.target.closest('[data-fdim]');
      if (drill) { fDim = drill.dataset.fdim; railQ[fDim] = ''; renderFiltersSheet(); return; }
      if (e.target.closest('[data-fback]')) { fDim = null; renderFiltersSheet(); return; }
      const row = e.target.closest('.prow[data-key]');
      if (row) { toggleValue(row.dataset.key, row.dataset.value, false); return; }
      // The standing tier. Its own handler here rather than the rail's, because
      // the rail is not in the DOM at this width.
      const st = e.target.closest('[data-standing]');
      if (st) { toggleStanding(st.dataset.standing); return; }
      if (e.target.closest('#st-reset')) { resetStanding(); }
    });
    sheet.addEventListener('input', (e) => {
      const box = e.target.closest('[data-railq]');
      if (!box) return;
      railQ[box.dataset.railq] = box.value;
      renderFiltersSheet();
    });
    return sheet;
  }

  /* ── Chips and the well ──────────────────────────────────────────────
     ONE builder for every chip. The mockup's round 2 had a second, near-copy
     renderer for typed words, which is why they read "Search mlb" while every
     other chip read "Category is X". */
  function chipHtml({ label, neg, value, rm, tip, cls = '', edit = '' }) {
    const ev = edit ? ` data-edit="${esc(edit)}"` : '';
    return `<span class="cs-chip${neg ? ' neg' : ''}${cls ? ` ${cls}` : ''}" data-tip="${tipAttr(tip)}">
      <span class="ck">${esc(label)}</span>
      <span class="cop">${neg ? 'is not' : 'is'}</span>
      <span class="cv"${ev}>${esc(value)}</span>
      <button class="cx" data-rm="${esc(rm)}" data-tip="Remove">&#10005;</button></span>`;
  }

  const dimByKey = (key) => (CAT ? CAT.dimensions.find((d) => d.key === key) : null) || null;
  const dimName = (key) => (dimByKey(key) || {}).label || key;

  function filterChipHtml(f, forceEx) {
    const list = forceEx ? f.ex : f.values;
    if (!list.length) return '';
    const labels = list.map((v) => String(dimLabel(f.key, v)));
    const shown = labels.slice(0, 2).join(', ') + (labels.length > 2 ? ` +${labels.length - 2}` : '');
    const dim = dimByKey(f.key);
    return chipHtml({
      label: dimName(f.key), neg: forceEx, value: shown,
      // A hidden dimension chips and negates like any other, but it has no value list to
      // open, so its chip is not editable - only removable.
      edit: dim && !dim.hidden ? f.key : '',
      rm: f.key + (forceEx ? ':ex' : ''),
      tip: `${dimName(f.key)} ${forceEx ? 'is not' : 'is'} ${labels.join(' or ')}\n`
         + "Values inside one filter are OR'd. Different filters are AND'd.",
    });
  }

  /* Typed text is ONE chip carrying the whole query (settled, mockup 23 round 6). Removing
     it clears the box, exactly as the x inside the box does, so the two cannot disagree
     about what "the text" is. The value is the raw text rather than a re-serialization of
     the parsed terms: the box already holds the authoritative spelling. */
  const textChipHtml = () => (state.q ? chipHtml({
    label: 'Text', neg: false, value: state.q, rm: '__alltext', cls: 'text',
    tip: 'Everything typed in the search box, as one condition.\nRemoving this clears the box.',
  }) : '');

  /* A PARKED chip: a filter belonging to the other grain, kept visible rather than
     hidden. Round 4 settled the spelling - "P2 -- Dimmed. Final answer." - and the
     argument was that `f.when=` stays in the URL on both grains, so hiding
     the chip would mean the URL carrying a filter that nothing on the page shows,
     which is exactly the shape of a silent behavior. Showing it is also what makes a
     toast unnecessary: a state you can see does not have to be narrated, and two
     toasts on every flip is a lot of talking about something you did on purpose.

     It is not editable and not negatable - there is nothing here to evaluate it
     against - but it IS removable, or a chip you cannot act on would be worse than
     one you cannot see. */
  function parkedChipHtml(f, grain) {
    const list = f.values.concat(f.ex);
    if (!list.length) return '';
    const labels = list.map((v) => String(dimLabel(f.key, v)));
    const shown = labels.slice(0, 2).join(', ') + (labels.length > 2 ? ` +${labels.length - 2}` : '');
    return chipHtml({
      label: dimName(f.key), neg: false, value: shown, cls: 'parked',
      rm: `${f.key}:parked:${grain}`,
      tip: `${dimName(f.key)} is ${labels.join(' or ')}\n\nThis filter belongs to `
         + `${grainUi(grain).label}, which is not the tab you are on, so it is not narrowing `
         + 'this list. Switch back and it applies again - or remove it here.',
    });
  }

  function renderChips() {
    const parkedHtml = Object.keys(parked)
      .map((g) => parked[g].map((f) => parkedChipHtml(f, g)).join('')).join('');
    const body = textChipHtml()
      + state.filters.map((f) => filterChipHtml(f, false) + filterChipHtml(f, true)).join('')
      + parkedHtml;
    // With text shown as one chip, "nothing filtered yet - showing all N" would be a lie the
    // moment you type, so the typed-in wording reports the live count and the well can never
    // claim more than the list is showing. With nothing typed it is a three-word prompt: the
    // count is already on the line under the bar, and saying it twice made this read like a
    // status line instead of an invitation.
    // last.total is null while fetchCounts() is still out (dev/changelog/598) - "0 of N"
    // would be a wrong claim, not a pending one.
    // esc()'d below, so this is plain text - no HTML entities.
    const empty = state.q
      ? (last.total === null
        ? 'no filters - counting matches…'
        : `no filters - ${nf(last.total)} of ${nf(CAT ? CAT.total_channels : 0)} channels match what you typed`)
      : 'add a filter';
    /* The row is LABELED, with its count on the label, and that replaces the old inline
       "Active filters" rather than joining it - two labels on one row is exactly the
       difference DESIGN.md §11.1 exists to catch (dev/changelog/810). The count is omitted
       at zero, where the row already says there are no filters. It lives in its own static
       slot because #wellbody is rewritten on every render. */
    const n = state.filters.reduce((sum, f) => sum + f.values.length + f.ex.length, 0)
      + (state.q ? 1 : 0);
    $('#well-lbl-slot').innerHTML = `<span class="well-lbl">Filters${
      n ? ` <span class="nsel">${nf(n)}</span>` : ''}</span>`;
    $('#wellbody').innerHTML = body || `<span class="well-none">${esc(empty)}</span>`;
    syncWellSlot(!!body);
  }

  /* The pill is the empty slot at the END of what is there: with nothing active it leads the
     line, with chips present it follows them. It is MOVED between two static slots, never
     duplicated - two buttons would mean two popovers holding the same pending dimension, and
     #wellbody is rewritten on every render so neither slot may live inside it. */
  function syncWellSlot(hasChips) {
    const pw = $('#addfpw2');
    const target = hasChips ? $('#addf-slot-tail') : $('#addf-slot-head');
    if (pw.parentNode !== target) target.appendChild(pw);
  }

  const wordsSegHtml = () => `<button data-wjoin="all" class="${state.matchAll ? 'on' : ''}"
      data-tip="Every word you type must match.">all words</button>
    <button data-wjoin="any" class="${state.matchAll ? '' : 'on'}"
      data-tip="Any one of the words you type may match. Wider.">any word</button>`;

  /* ── Match mode ──────────────────────────────────────────────────────
     ONE control, joined to the box's LEFT edge and sharing its border, wearing the app's
     own [data-menu] portal (dev/changelog/807). It qualifies what you are typing rather
     than changing what a row is, so it reads as part of the box - "all words: <what you
     type>" - and it hands the whole right of the line to Saved searches.

     ONE LIVE COPY AT EACH WIDTH, and the segmented spelling that used to sit on this line
     is GONE rather than hidden. It was kept for one commit on the theory that the
     Searching-in sheet read it; the sheet builds its own from `wordsSegHtml()`, so the one
     on the line was a second live control sitting beside this one on desktop - which is
     precisely the duplication §5.4 removed from the chip row (dev/changelog/811). */
  const WJOIN_LABEL = { all: 'All words', any: 'Any word' };
  const WJOIN_TIP = {
    all: 'Every word you type must match.',
    any: 'Any one of the words you type may match. Wider.',
  };

  function renderWordsSeg() {
    const join = $('#wjoin-left');
    if (!join) return;
    const sline = $('#sline');
    if (sline) sline.classList.add('joined');
    const cur = state.matchAll ? 'all' : 'any';
    join.innerHTML = `<span class="menu-wrap">
      <button class="btn btn-sm wjoin" type="button" data-menu
        data-tip="${tipAttr(`${WJOIN_TIP[cur]}\n\nWhich of the words you type have to match. `
          + 'It is a property of the SEARCH, not a filter.')}">${esc(WJOIN_LABEL[cur])} &#9662;</button>
      <div class="menu">${Object.keys(WJOIN_LABEL).map((k) =>
        `<button class="menu-item" type="button" data-wjoin="${k}">${esc(WJOIN_LABEL[k])}</button>`).join('')}</div>
    </span>`;
  }

  /* ── The chip row: every entry point on one line (DESIGN.md 9.4) ──────
     Phone only. It REPLACES the toolbar rather than joining it - Saved, Clear
     all and + Filter are all in here at this width, because two live copies of
     one control is how the two come to disagree. Each chip opens a sheet, and
     each one carries the state of what it opens (how many filters, how many
     scope fields, which sort) so the row reads as a summary rather than a menu
     bar. */
  const sortIsDefault = () =>
    !!CAT && state.sort === defaultSortFor() && !state.sortDesc;
  const scopeIsDefault = () => {
    if (!CAT || !state.matchAll) return false;
    const def = defaultFieldsFor();
    return state.fields.length === def.length && def.every((k) => state.fields.includes(k));
  };

  function renderChipRow() {
    const row = $('#chiprow');
    // One button, two things: the table's Columns popover up there, the card's
    // Fields sheet down here. A second button would be a control that exists at
    // one width only and confuses the other.
    $('#cols-btn').innerHTML = isMobile() ? '&#9881; Fields' : '&#9881; Columns';
    if (!CAT || !isMobile()) { row.innerHTML = ''; return; }
    const st = savedStatus();
    const bits = [];
    /* NO Filters CHIP AND NO Clear all HERE. Both live in the well at every width, which
       is where this design departs from the shipped chip row on purpose: production put
       them here BECAUSE its toolbar is hidden below the breakpoint, and the well is on
       screen at both. Carrying them again is two live copies of one control - which is
       exactly what a first port of this row shipped, a visible duplicate Clear all
       (DESIGN-group-search-rows.md §5.4). The row is Searching in, Sort and Saved
       searches; the well's own + Filter opens the Filters sheet. */
    bits.push(`<button class="mchip${scopeIsDefault() ? '' : ' on'}" data-mchip="scope"
      data-tip="${tipAttr(`Searching ${fieldLabels().join(', ')}.\n${SCOPE_WHY}`)}"
      >Searching in<span class="mn">${state.fields.length}</span></button>`);
    bits.push(`<button class="mchip${sortIsDefault() ? '' : ' on'}" data-mchip="sort"
      data-tip="Sort the list. There are no column headers at this width, so this is where sorting lives."
      >Sort: ${esc(sortLabel(state.sort))} ${state.sortDesc ? '&#9660;' : '&#9650;'}</button>`);
    // Its full name and its disk glyph, not the abbreviation: nothing on this row needs
    // the four characters back (DESIGN-group-search-rows.md §5.4).
    bits.push(`<button class="mchip${st.name ? ' on' : ''}" data-mchip="saved"
      data-tip="${tipAttr('Saved searches.\nSaves the text, the match mode, the search scope, '
        + 'the standing options and every filter chip under a name.')}">&#128190; Saved searches</button>`);
    if (st.name) {
      bits.push(`<span class="sf-name">${esc(st.name)}</span>`);
      if (st.dirty) {
        bits.push('<span class="sf-dirty" data-tip="This saved search has been changed since it '
          + 'was loaded. Save it again to keep the change.">&#9679; edited</span>');
      }
    }
    row.innerHTML = bits.join('');
  }

  /* ── Search in: the scope pane ───────────────────────────────────────
     WHICH FIELDS THE TEXT IS MATCHED AGAINST IS A PROPERTY OF THE SEARCH BOX,
     not of the filter rail (mockup 21 round 8) - so it lives in the menu
     you already get by clicking into the box. The rail card that used to hold it
     is deleted; dev/changelog/387 has it if it is ever wanted back.

     The groups are HEADINGS, not controls: every field is a leaf you switch on
     or off. The parent-control spelling forced a half-on state onto a switch
     that has none, and round 10 deleted it.

     Two things this placement owes the user, because it is only on screen while
     the box has focus, and both are implemented below rather than assumed:
     the mousedown preventDefault on #sugg, and the blur guard + the focus() in
     the [data-field] handler. Neither alone is sufficient. */
  const SCOPE_WHY = 'Which fields the typed text is matched against. It is a property of the '
    + 'SEARCH, not a filter - a filter narrows what is listed, this decides what counts as a '
    + 'match in the first place.';

  function fieldRowHtml(f) {
    const on = state.fields.includes(f.key);
    // The switch is OUTSIDE the text: a <label> containing the input would toggle it twice
    // per click on some browsers, and .switch is itself the label for its own input.
    return `<div class="prow">
      <label class="switch"><input type="checkbox" data-field="${esc(f.key)}"${on ? ' checked' : ''}><span class="knob"></span></label>
      <span class="pv">${esc(f.label)}</span>${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ''}</div>`;
  }

  function fieldRowsHtml() {
    if (!CAT) return '';
    return (CAT.field_groups || []).map((g) => {
      const fields = CAT.fields.filter((f) => f.group === g.name);
      if (!fields.length) return '';
      const what = g.help ? ` <span class="fwhat" data-tip="${tipAttr(g.help)}">&#9432;</span>` : '';
      // Derived from the group's own fields rather than declared, so a group that gains or
      // loses examples cannot end up with a column title over an empty column.
      const ex = fields.some((f) => f.hint) ? '<span class="fex">Example</span>' : '';
      return `<div class="fgroup">${esc(g.name)}${what}${ex}</div>` + fields.map(fieldRowHtml).join('');
    }).join('');
  }

  /* Every surface writes through here, so "something has to be searched" is one rule rather
     than one per idiom. The result is re-ordered into REGISTRY order, not click order: `in`
     is part of the URL, and a shared link should not depend on which switch was flipped
     first. */
  function setField(key, on) {
    const want = new Set(state.fields);
    if (on) want.add(key); else want.delete(key);
    if (!want.size) {
      want.add('name');
      showToast('Something has to be searched, so Channel name stayed on.');
    }
    state.fields = CAT.fields.filter((f) => want.has(f.key)).map((f) => f.key);
    // A scope the user has touched is theirs and crosses a grain flip unchanged, exactly as
    // a sort they picked does. Never cleared: re-ticking the default set by hand is still a
    // choice, and setGrain() replacing it later would be this page overruling them.
    state.fieldsExplicit = true;
    applyNow();
  }

  const scopeSuggHtml = () => `<div class="sugg-scope" id="suggscope">
    <div class="sgroup stitle">Searching in <span class="scnt">${state.fields.length}</span>
      <span class="fwhat" data-tip="${tipAttr(SCOPE_WHY)}">&#9432;</span></div>
    ${fieldRowsHtml()}</div>`;

  /* ── The Searching-in sheet ──────────────────────────────────────────
     At 375 the popover's two panes cannot sit side by side, and STACKING them
     would push every suggestion below the fold on each keystroke - which is the
     height complaint the two-pane layout was invented to fix, reintroduced at a
     quarter of the width. So the panes split by FUNCTION instead: the scope
     becomes this sheet, and the suggestions stay under the box where they have
     to be. The all-words/any-word segment comes with it - it is a property of
     the search in exactly the way the field scope is, and it can no longer sit
     beside a 375px box.

     ONE THING GETS SIMPLER BY MOVING, and it must not be re-solved here: a sheet
     is not focus-dependent, so the popover's mousedown-preventDefault, its blur
     guard and its refocus dance have no counterpart down here. */
  const scopeSheetTitle = () =>
    `Searching in (${state.fields.length} of ${CAT ? CAT.fields.length : 0})`;

  const scopeSheetBody = () => `<div class="fgroup">Match</div>
    <div class="seg">${wordsSegHtml()}</div>
    <div class="sh-note">${state.matchAll
      ? 'Every word you type must match.'
      : 'Any one of the words you type may match. Wider.'}</div>
    ${fieldRowsHtml()}
    <div class="sh-note">${esc(SCOPE_WHY)}</div>`;

  function openScopeSheet() {
    openSheet({
      kind: 'scope', title: scopeSheetTitle(), body: scopeSheetBody(),
      redraw: () => { setSheetTitle(scopeSheetTitle()); sheetBody().innerHTML = scopeSheetBody(); },
    });
  }

  /* ── The Sort sheet ──────────────────────────────────────────────────
     The table's sortable headers go away with the table, so this is the entry
     point (DESIGN.md 9.4's Sort chip). Rendered in the TABLE's column order
     rather than the catalog's alphabetical key order, so the list reads the same
     left-to-right as the headers it replaces - and only the SORTS registry's own
     keys appear, which is what keeps the three columns that look sortable and are
     not (Now airing, Groups, Tags) off it for free.

     colList()/grainUi() rather than the channel grain's COLUMNS/'name' literally -
     this is the ACTIVE grain's registry and pinned column, the same two helpers
     guardSortAgainstHidden() already uses. Getting this wrong silently drops the
     airing grain's own sorts (When, Channel, Program) from the sheet since none of
     them exist in the channel grain's COLUMNS, which is what made "When" (start
     time) unreachable on mobile in EPG search (dev/docs/BUGS.md). */
  const sortKeys = () => [grainUi().pinSort].concat(colList().map((c) => c.sort)).filter(canSort);

  const sortSheetBody = () => sortKeys().map((k) => {
    const on = state.sort === k;
    return `<div class="sort-opt${on ? ' on-field' : ''}" data-sortkey="${esc(k)}">
      <span class="arrow">${on ? (state.sortDesc ? '&#9660;' : '&#9650;') : ''}</span>
      <span class="pv">${esc(sortLabel(k))}</span>
      <span class="dir">${esc(sortDirWords(k, on ? state.sortDesc : false))}</span></div>`;
  }).join('')
    + `<div class="sh-note">Every field sorts both ways - tap the field it is already sorted by to
       reverse it, and a different field starts ascending, exactly as clicking a column header
       does at a wider window.</div>`;

  function openSortSheet() {
    const sheet = openSheet({
      kind: 'sort', title: `Sort ${grainUi().noun}s`, body: sortSheetBody(),
      redraw: () => { sheetBody().innerHTML = sortSheetBody(); },
    });
    sheet.addEventListener('click', (e) => {
      const opt = e.target.closest('[data-sortkey]');
      if (!opt) return;
      setSort(opt.dataset.sortkey);
    });
  }

  /* One rule for both surfaces: re-picking the active field flips it, a different
     field starts ascending. */
  function setSort(key) {
    if (state.sort === key) state.sortDesc = !state.sortDesc;
    else { state.sort = key; state.sortDesc = false; }
    // Chosen, so it now crosses a grain flip - see setGrain().
    state.sortExplicit = true;
    // Picking a sort answers the "your sort could not come with you" note, so it goes.
    state.sortNote = null;
    applyNow();
  }

  /* ── Suggestions: the other half of the on-ramp ───────────────────────
     Counts come from the LAST completed search rather than a fresh one: the menu
     redraws on every keystroke and the results are debounced, so asking the
     server here would be a second request per character to be one debounce tick
     fresher. A count that has not been asked for yet is `--`, never 0. */
  /* `suggOpen` is a state, not a class read back off the DOM, and it is what makes a close
     STICK: renderSugg() runs from renderBar() on every landing response, so without it a
     search answering inside the 140ms blur window would re-open the menu behind the popover
     that just replaced it. Focusing or typing in the box is what sets it again. */
  let suggIdx = -1, suggItems = [], suggOpen = false;
  const SUGG_VALUES = 6;         // filter-value suggestions offered at once
  const SUGG_CHANNELS = 4;       // "this exact channel" rows offered at once
  const SUGG_GAP = 14;           // breathing room below the menu
  const SUGG_MIN = 260;          // never squeeze it past useless
  // Presentation only, so it is not in the catalog - a dimension added later gets the
  // fallback rather than a blank cell.
  const DIM_ICON = { tag: '&#9873;', acct: '&#9679;', health: '&#9829;', group: '&#9707;',
                     cat: '&#9636;', other: '&#9881;', chan: '&#9654;' };
  const dimIcon = (key) => DIM_ICON[key] || '&#9679;';
  const SYNTAX_HTML = `<div class="sugg-syn">
    <code>-word</code> excludes it &#183; <code>*</code> and <code>?</code> are wildcards,
    e.g. <code>fox*</code> &#183; <code>"two words"</code> stays together</div>`;
  const countHint = (key, value) => {
    const c = countOf(key, value);
    return c === null ? '--' : nf(c);
  };

  function buildSuggestions(raw) {
    if (!CAT) return [];
    const visible = dimsFor().filter((d) => !d.hidden);
    const out = [];
    if (!raw.trim()) {
      out.push({ group: 'Start with a filter', kind: 'dim', label: 'Browse all filters',
                 dim: null, ico: '&#43;' });
      visible.slice(0, 4).forEach((d) =>
        out.push({ kind: 'dim', label: d.label, dim: d.key, ico: dimIcon(d.key) }));
      return out;
    }
    out.push({ group: 'Search', kind: 'text', label: raw.trim(), ico: '&#128269;',
               hint: nf(last.total) });
    const parsed = parseQuery(raw);
    const term = (parsed.inc[parsed.inc.length - 1] || '').toLowerCase();
    if (!term) return out;

    // Stops at SUGG_VALUES rather than collecting every match and slicing: on this database
    // the category vocabulary alone is 1,722 values and this runs on every keystroke.
    const vals = [];
    for (const d of visible) {
      for (const v of dimValues(d.key)) {
        if (vals.length >= SUGG_VALUES) break;
        const label = String(dimLabel(d.key, v));
        // A genuine 0 (as opposed to null, "not counted yet") means this value would
        // return nothing under the filters already active - don't offer it as a suggestion.
        if (label.toLowerCase().includes(term) && countOf(d.key, v) !== 0) {
          vals.push({ kind: 'val', dim: d.key, val: v, label, ico: dimIcon(d.key),
                      key: d.label, hint: countHint(d.key, v) });
        }
      }
      if (vals.length >= SUGG_VALUES) break;
    }
    if (vals.length) { vals[0].group = 'Filter by'; out.push(...vals); }

    /* Matching a specific channel narrows to THAT channel, by id - a search box that
       navigates somewhere is unexpected (mockup 23 round 2).

       THE ROWS ARE THE SOURCE, not a scan of the catalog. The mockup could scan its 12,351
       seeded channels; production has 136,130 and this page holds one page of them. The
       honest consequence is that a channel matching only on a later page is not offered
       here - the rail and the text search are how you reach it. */
    pageChannels().filter((c) => String(c.name || '').toLowerCase().includes(term))
      .slice(0, SUGG_CHANNELS)
      .forEach((c, i) => out.push({
        group: i === 0 ? 'This exact channel' : null, kind: 'val', dim: 'chan',
        val: String(c.id), label: c.name, ico: dimIcon('chan'), key: 'Channel',
        hint: c.category || '',
      }));
    return out;
  }

  function renderSugg() {
    const el = $('#sugg');
    if (!CAT || !suggOpen || !$('#sbox').classList.contains('focus')) {
      el.classList.remove('show');
      return;
    }
    suggItems = buildSuggestions($('#cs-q').value);
    const phone = isMobile();
    // Search in lives in this menu, so "no suggestions" must NOT close it - that is exactly
    // when someone is most likely to be widening their scope. There is no early return.
    const rows = suggItems.map((s, i) => {
      const g = s.group ? `<div class="sgroup">${esc(s.group)}</div>` : '';
      const hint = s.hint ? `<span class="shint">${esc(s.hint)}</span>` : '';
      const key = s.key ? `<span class="skey">${esc(s.key)}:</span> ` : '';
      // The three-state is here too, so an "is not" can be added straight from the box. The
      // index is the handle, so this is unaffected by the no-delimiter rule that governs the
      // value rows themselves.
      // NOT at phone width: a +/- pair on every suggestion row leaves about 150px for the
      // label, so down there a suggestion is one tap that INCLUDES, and excluding is either
      // the -word syntax the footer teaches or the three-state in the Filters sheet, where
      // the row is full width.
      let ctl = '';
      if (!phone && (s.kind === 'val' || s.kind === 'text')) {
        const st = s.kind === 'val' ? valueState(s.dim, s.val) : 0;
        ctl = `<span class="tri"><button data-si-inc="${i}" class="${st === 1 ? 'on-inc' : ''}"
          data-tip="Include this">+</button><button data-si-exc="${i}" class="${st === -1 ? 'on-exc' : ''}"
          data-tip="Exclude this">&minus;</button></span>`;
      }
      const kbd = i === suggIdx ? ' <kbd>enter</kbd>' : '';
      return `${g}<div class="sitem${i === suggIdx ? ' sel' : ''}" data-si="${i}">
        <span class="sico">${s.ico}</span><span class="stxt">${key}${esc(s.label)}</span>${hint}${ctl}${kbd}</div>`;
    }).join('');
    /* One pane at phone width: the scope pane is a sheet down there, so what is left is the
       rows plus two footers - the syntax help, and a line naming the current scope with a way
       into that sheet. THAT FOOTER IS OWED: it is the only thing keeping the link between what
       you are typing and what it matches after the switches moved out of view. */
    const scopeFoot = phone
      ? `<div class="sugg-scopeline" data-openscope="1"><span>Searching in</span>
          <span class="ssl-v">${esc(fieldLabels().join(', '))}</span>
          <span class="ssl-go">change</span></div>`
      : '';
    el.innerHTML = (phone
      ? `<div class="sugg-main">${rows}</div>`
      : `<div class="sugg-2col">${scopeSuggHtml()}<div class="sugg-main">${rows}</div></div>`)
      + scopeFoot + SYNTAX_HTML;
    el.classList.add('show');
    sizeSugg(el);
  }

  /* "I'd like if the popover showed the full thing without requiring scrolling." A
     flat max-height cannot promise that - the content runs from six rows to twenty and the
     room under the box depends on the window and on how far the sticky bar has scrolled. So
     the limit is MEASURED and the CSS value is only the pre-JS floor. A zero rect means
     "no layout engine here", which leaves the CSS value alone rather than collapsing it. */
  function sizeSugg(el) {
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    if (!r || !r.top) return;
    el.style.maxHeight = `${Math.max(SUGG_MIN, window.innerHeight - r.top - SUGG_GAP)}px`;
  }
  const closeSugg = () => { suggIdx = -1; suggOpen = false; $('#sugg').classList.remove('show'); };
  const negTerm = (t) => `-${/\s/.test(t) ? `"${t}"` : t}`;

  function applySugg(s, asEx) {
    const box = $('#cs-q');
    if (s.kind === 'text') {
      if (asEx) {
        // Excluding what you typed rewrites it in the -term syntax, so the box teaches the
        // syntax rather than hiding it behind a button. Quotes are part of the term and have
        // to survive the rewrite.
        const p = parseQuery(box.value);
        box.value = p.inc.concat(p.ex).map(negTerm).join(' ');
      }
      state.q = box.value.trim();
    } else if (s.kind === 'val') {
      closeSugg();
      box.value = '';
      state.q = '';
      toggleValue(s.dim, s.val, !!asEx);      // applies and re-renders
      return;
    } else if (s.kind === 'dim') {
      // The suggestion menu outranks the popover it is about to open, so it closes FIRST -
      // otherwise + Filter opens behind it (mockup 23 round 3).
      closeSugg();
      // At phone width the same tap lands on the Filters SHEET, at the same level: a tap on
      // "Account" opens accounts, not the list of dimensions.
      if (isMobile()) { box.blur(); openFiltersSheet(s.dim); }
      else openFilterPop($('#addfpw'), s.dim);
      return;
    }
    closeSugg();
    applyNow();
  }

  /* ── The + Filter popover: dimension list -> value picker -> chip ─────
     ONE panel, moved between its two triggers (the toolbar button and the pill in the well)
     rather than a second copy that would hold its own pending dimension and its own scroll
     position. util.js owns opening, closing and positioning it; this owns its contents. */
  let popDim = null, popQ = '';

  function renderFilterPop({ reposition = false } = {}) {
    const body = $('#fb-pop-body');
    if (!body || !CAT) return;
    // The panel is rebuilt whole and a search response can land while the caret is in its
    // find box, so which box was focused is read off the DOM - the same reason renderRail()
    // does it rather than taking it from whichever caller triggered the render.
    const active = document.activeElement;
    const caret = active && active.dataset && active.dataset.popq !== undefined
      ? active.selectionStart : null;

    if (!popDim) {
      body.innerHTML = '<h5>Add a filter</h5>'
        + dimsFor().filter((d) => !d.hidden).map((d) => {
          const f = findF(d.key);
          const n = f ? f.values.length + f.ex.length : 0;
          return `<div class="prow" data-dim="${esc(d.key)}" style="cursor:pointer">
            <span style="width:16px">${dimIcon(d.key)}</span><span class="pv">${esc(d.label)}</span>
            ${n ? `<span class="pcount">${n} set</span>` : ''}</div>`;
        }).join('')
        + `<div class="pnote">Values inside one filter are OR'd together. Different filters
           are AND'd. Each value is off, included or excluded, right where you are looking at it.</div>`;
    } else {
      const dim = dimByKey(popDim) || { key: popDim, label: popDim, help: '' };
      const all = dimValues(popDim);
      const q = popQ.trim().toLowerCase();
      // Filtered here rather than hidden in place: the caret is restored explicitly below,
      // which is what the hide-in-place spelling existed to avoid needing.
      const values = q
        ? all.filter((v) => String(dimLabel(popDim, v)).toLowerCase().includes(q)) : all;
      body.innerHTML = `<button class="pop-back" data-back="1">&#8592; All filters</button>
        <h5>${esc(dim.label)}</h5>
        ${all.length > RAIL_SHOW ? findBoxHtml('pop-search', popQ, `Filter ${dim.label.toLowerCase()}...`, '__pop__', 'popq') : ''}
        <div class="pop-scroll">${values.map((v) =>
          valueRowHtml(popDim, v, String(dimLabel(popDim, v)), countOf(popDim, v))).join('')
          || '<div class="fac-none">no value matches that</div>'}</div>
        ${dim.help ? `<div class="pnote">${esc(dim.help)}</div>` : ''}`;
    }

    if (caret !== null) {
      const inp = $('#fb-pop [data-popq]');
      if (inp) {
        inp.focus();
        try { inp.setSelectionRange(caret, caret); } catch (e) { /* no selection API here */ }
      }
    }
    // Only where the height really changes (navigating between the two views). Re-anchoring
    // on every count refresh would make an open menu jump under the pointer.
    const menu = $('#fb-pop');
    const trigger = menu.parentNode && menu.parentNode.querySelector('[data-menu]');
    if (reposition && trigger && menu.classList.contains('open')) positionMenu(menu, trigger);
  }

  // util.js's [data-menu] handler looks for a `.menu` inside the clicked button's OWN
  // .menu-wrap, and only #addfpw contains one - so the panel has to be sitting in the right
  // wrap before that handler runs. See the capture-phase listener in the wiring below.
  function moveFilterPop(wrap) {
    const menu = $('#fb-pop');
    if (menu.parentNode !== wrap) wrap.appendChild(menu);
    return menu;
  }

  function openFilterPop(wrap, dim) {
    popDim = dim || null;
    popQ = '';
    const menu = moveFilterPop(wrap);
    renderFilterPop();
    closeMenus();                              // util.js: one menu open at a time
    menu.classList.add('open');
    positionMenu(menu, wrap.querySelector('[data-menu]'));
    syncScrollLock();
  }

  /* ── One render for everything above the results ─────────────────────
     Called both from applyNow() (immediately, off local state) and from
     renderResults() (the well's count line and the Search suggestion read
     last.total, which only exists once the rows land). */
  function renderBar() {
    if (!CAT) return;
    renderChips();
    renderWordsSeg();
    // Phone only; a no-op above the breakpoint, where #tbar is the toolbar.
    renderChipRow();
    renderSugg();                              // no-ops unless the box has focus
    // Which saved search this is, and whether it has been edited since - derived
    // from the state itself, so it tracks every change without a call site.
    renderSavedState();
    if ($('#fb-pop').classList.contains('open')) renderFilterPop();
    // A sheet applying live is behind a scrim, so it has to redraw when the model
    // moves under it or the tap that moved it looks like it did nothing.
    renderOpenSheet();
  }

  /* ── Wiring ──────────────────────────────────────────────────────── */
  const qbox = $('#cs-q');

  qbox.addEventListener('input', () => {
    state.q = qbox.value.trim();
    $('#sb-clr').style.display = state.q ? '' : 'none';
    suggIdx = -1;
    suggOpen = true;                           // typing re-opens it after an Enter closed it
    renderSugg();                              // the menu tracks typing; the search is debounced
    scheduleApply();
  });
  $('#sb-clr').addEventListener('click', () => {
    qbox.value = '';
    state.q = '';
    qbox.focus();
    applyNow();
  });

  qbox.addEventListener('focus', () => {
    $('#sbox').classList.add('focus');
    suggOpen = true;
    renderSugg();
  });
  /* THE MENU IS SHOWN ONLY WHILE #sbox CARRIES .focus, and this takes it away 140ms after
     the box blurs. THE GUARD IS NOT OPTIONAL, and believing the mousedown preventDefault
     below was sufficient is what shipped the mockup's "closes on select, not on deselect"
     defect: a <label>'s activation behaviour focuses its own control on CLICK, which
     mousedown cannot prevent, so ticking a scope switch blurs the box. The [data-field]
     handler puts the caret straight back; without this guard the already-queued timer would
     undo that a moment later and shut the menu anyway. */
  qbox.addEventListener('blur', () => {
    setTimeout(() => {
      if (document.activeElement === qbox) return;
      $('#sbox').classList.remove('focus');
      renderSugg();
    }, 140);
  });
  qbox.addEventListener('keydown', (e) => {
    const open = $('#sugg').classList.contains('show');
    if (e.key === 'ArrowDown' && open) {
      e.preventDefault();
      suggIdx = Math.min(suggIdx + 1, suggItems.length - 1);
      renderSugg();
    } else if (e.key === 'ArrowUp' && open) {
      e.preventDefault();
      suggIdx = Math.max(suggIdx - 1, -1);
      renderSugg();
    } else if (e.key === 'Enter') {
      if (open && suggIdx >= 0) { e.preventDefault(); applySugg(suggItems[suggIdx], false); }
      else { closeSugg(); applyNow(); }
    }
  });

  /* THE SUGGESTION MENU'S ONE REAL HAZARD. It is shown only while the input has focus, so
     mousing down on anything inside it would blur the input and dismiss the menu out from
     under the click. preventDefault on MOUSEDOWN stops the POINTER from moving focus; the
     click still fires afterwards, so a scope switch toggles natively and the global
     [data-field] change handler does the work. Do not "simplify" this into a click handler -
     by click time the blur has already been queued. */
  $('#sugg').addEventListener('mousedown', (e) => {
    if (e.target.closest('#suggscope')) { e.preventDefault(); return; }
    /* The scope footer deliberately DOES let the box blur: it is opening a sheet, so the
       menu should go with it - and on a phone leaving the caret in the box would leave the
       keyboard up over the sheet that just opened. */
    if (e.target.closest('[data-openscope]')) {
      e.preventDefault();
      qbox.blur();
      closeSugg();
      openScopeSheet();
      return;
    }
    const inc = e.target.closest('[data-si-inc]');
    if (inc) { e.preventDefault(); applySugg(suggItems[Number(inc.dataset.siInc)], false); return; }
    const exc = e.target.closest('[data-si-exc]');
    if (exc) { e.preventDefault(); applySugg(suggItems[Number(exc.dataset.siExc)], true); return; }
    const item = e.target.closest('[data-si]');
    if (!item) return;
    e.preventDefault();
    applySugg(suggItems[Number(item.dataset.si)], false);
  });
  /* The menu is driven on MOUSEDOWN (above), but the click still fires afterwards and would
     reach util.js's document closer - which, seeing a click that is neither a trigger nor
     inside a `.menu`, closes every open menu. That would shut the + Filter popover a
     suggestion had just opened. #sugg is not a `.menu`, so it has to say so itself. */
  $('#sugg').addEventListener('click', (e) => e.stopPropagation());

  /* The scope switches, wherever the menu happens to be in the DOM. Read the control, never
     toggle a parallel copy of its state, or the switch and the model can disagree about what
     is on. The focus() is the other half of the blur guard above and must come BEFORE
     setField, so the re-render setField triggers already sees the box focused. */
  document.addEventListener('change', (e) => {
    const sw = e.target.closest('[data-field]');
    if (!sw) return;
    /* The refocus is the other half of the desktop popover's blur guard, and it has no
       counterpart in the sheet: a sheet is not focus-dependent, and putting the caret back
       in the box down there would raise the on-screen keyboard over the sheet being used. */
    if (!isMobile()) qbox.focus();
    setField(sw.dataset.field, sw.checked);
  });

  /* ONE popover, TWO triggers. util.js resolves a [data-menu] click to the `.menu` inside
     that button's own .menu-wrap, and only #addfpw holds one - so the panel is moved to
     whichever trigger was clicked, on the CAPTURE phase, before util.js's bubble-phase
     handler looks for it. A second panel is not the alternative: it would mean two popovers
     holding the same pending dimension.

     The same listener closes the suggestion menu. #sugg is not a .menu, so util.js's closer
     never sees it, and an overlay opening in front of a menu that stays up behind it is
     exactly what the mobile round caught. */
  document.addEventListener('click', (e) => {
    const trigger = e.target.closest('[data-menu]');
    if (!trigger) return;
    closeSugg();
    /* Below the breakpoint two of these popovers are sheets instead, so the click
       is taken away from util.js before its bubble-phase handler ever sees it.
       (#fb-add and #sf-btn are inside the hidden toolbar down there, so they are
       not reachable and need no branch of their own.) */
    if (isMobile()) {
      /* NO TAP-AGAIN-TO-CLOSE HERE, and it is not an oversight. A chip can toggle its own
         sheet because the chip row stays above it; these two triggers sit in the page
         BODY, and an open sheet covers them - a browser pass confirmed the sheet's own
         content is what a tap at those coordinates reaches, so a toggle branch here could
         never fire. The sheet closes by its ✕ and by its scrim, which is what it has
         always done for both of these. */
      const sheetFor = { 'cols-btn': openFieldsSheet, addf2: () => openFiltersSheet(null) }[trigger.id];
      if (sheetFor) { e.preventDefault(); e.stopPropagation(); sheetFor(); return; }
    }
    // Rendered before util.js opens it, so the panel is never shown holding the
    // list as it was the last time it was up.
    if (trigger.id === 'sf-btn') { sfName = loadedSaved || ''; sfError = ''; renderSavedPop(); return; }
    if (trigger.id !== 'fb-add' && trigger.id !== 'addf2') return;
    const wrap = trigger.closest('.menu-wrap');
    const menu = $('#fb-pop');
    // Clicking the OTHER trigger while it is open re-anchors it there rather than closing
    // it: util.js reads "was it already open" to decide, and without this the panel would
    // shut while sitting under a button that was just asked to show it.
    if (menu.parentNode !== wrap && menu.classList.contains('open')) closeMenus();
    moveFilterPop(wrap);
    // Opening always starts at the dimension list; a stale pending dimension from the last
    // time it was open would answer a question nobody asked.
    if (!menu.classList.contains('open')) { popDim = null; popQ = ''; renderFilterPop(); }
  }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSugg(); });

  /* The popover's own contents. Attached to the panel rather than to the document because
     the panel MOVES between the two wraps, and because util.js deliberately leaves a menu
     open on a click that is not a link or a .menu-item - which is what keeps a value picker
     usable. */
  $('#fb-pop').addEventListener('click', (e) => {
    /* THE PANEL OWNS ITS OWN CLICKS, and this line is why: util.js keeps a menu open by
       testing `e.target.closest('.menu.open')`, and every branch below rewrites
       #fb-pop-body - which DETACHES the clicked node, so that test finds no ancestors, the
       document closer decides the click landed outside every menu, and the popover shuts
       the instant you pick anything in it. Nothing in this panel is a link or a
       .menu-item, so there is no behaviour of util.js's left to want here. */
    e.stopPropagation();
    const tri = e.target.closest('.tri button[data-key]');
    if (tri) { toggleValue(tri.dataset.key, tri.dataset.value, tri.dataset.dir === 'exc'); return; }
    const clr = e.target.closest('[data-findclr]');
    if (clr) { popQ = ''; renderFilterPop(); return; }
    const dim = e.target.closest('[data-dim]');
    if (dim) { popDim = dim.dataset.dim; popQ = ''; renderFilterPop({ reposition: true }); return; }
    if (e.target.closest('[data-back]')) {
      popDim = null; popQ = '';
      renderFilterPop({ reposition: true });
      return;
    }
    const row = e.target.closest('.prow[data-key]');
    if (row) toggleValue(row.dataset.key, row.dataset.value, false);
  });
  $('#fb-pop').addEventListener('input', (e) => {
    const box = e.target.closest('[data-popq]');
    if (!box) return;
    popQ = box.value;
    renderFilterPop();
  });

  /* Every branch of the Saved surface, once, for both arrangements. `redraw` is how
     each one repaints itself: the popover rebuilds #sf-pop-body, the sheet rebuilds
     its own body. The popover's binding below stops propagation because it rebuilds
     itself on every branch, which DETACHES the clicked node - util.js's
     `closest('.menu.open')` test then finds no ancestors and its document closer
     would shut the panel on every pick. */
  function savedAction(e, redraw) {
    if (e.target.closest('[data-save]')) { saveCurrentSearch(); return; }

    const del = e.target.closest('[data-del]');
    if (del) {
      const idx = Number(del.dataset.del);
      const [gone] = savedSearches.splice(idx, 1);
      if (!gone) return;
      const prevLoadedSaved = loadedSaved;
      if (loadedSaved === gone.name) loadedSaved = null;
      persistSaved(() => {
        savedSearches.splice(idx, 0, gone);
        loadedSaved = prevLoadedSaved;
      });
      redraw();
      renderSavedState();
      renderChipRow();
      showToast(`Deleted "${gone.name}".`);
      return;
    }

    // One default at a time, and it can be turned back off - a page that could
    // only ever be given a new opening search, never none, would have no way back
    // to a bare /channels.
    const def = e.target.closest('[data-default]');
    if (def) {
      const i = Number(def.dataset.default);
      const s = savedSearches[i];
      if (!s) return;
      const on = !s.is_default;
      const prevFlags = savedSearches.map((x) => x.is_default);
      savedSearches.forEach((x, j) => { x.is_default = on && j === i; });
      persistSaved(() => {
        savedSearches.forEach((x, j) => { x.is_default = prevFlags[j]; });
      });
      redraw();
      showToast(on
        ? `"${s.name}" is what this page opens with now. The address bar will show that `
          + 'search too - the server redirects to it rather than applying it silently.'
        : `"${s.name}" is no longer the default. This page opens with everything again.`);
      return;
    }

    const load = e.target.closest('[data-load]');
    if (load) {
      const s = savedSearches[Number(load.dataset.load)];
      if (!s) return;
      loadedSaved = s.name;
      sfName = s.name;
      // The way back out of a duplicate drill-in is about a search the user has
      // now deliberately left, so it goes with it rather than sitting there
      // offering to undo something else.
      searchSnapshot = null;
      closeMenus();
      closeSheet();
      restore(s.params);
      showToast(`Loaded "${s.name}".`);
    }
  }

  $('#sf-pop').addEventListener('click', (e) => {
    e.stopPropagation();
    savedAction(e, renderSavedPop);
  });
  $('#sf-pop').addEventListener('input', (e) => {
    const box = e.target.closest('[data-sfname]');
    if (box) sfName = box.value;
  });
  $('#sf-pop').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.closest('[data-sfname]')) {
      e.preventDefault();
      saveCurrentSearch();
    }
  });

  /* The well: chip removal, and the chip value that reopens its own value picker. Clicks
     inside the popover are exempt because the pill's wrap - and therefore the panel - sits
     inside the well whenever there are chips. */
  $('#well').addEventListener('click', (e) => {
    if (e.target.closest('.menu')) return;
    const rm = e.target.closest('[data-rm]');
    if (rm) {
      const key = rm.dataset.rm;
      // The one-chip spelling carries the whole query, so removing it means what the x
      // inside the box means - not "drop one term".
      if (key === '__alltext') {
        qbox.value = '';
        state.q = '';
        $('#sb-clr').style.display = 'none';
      } else if (key.indexOf(':parked:') > 0) {
        // A parked chip is not in state.filters, so it is removed from the park itself.
        // Nothing is re-run: it was not narrowing this list, so dropping it cannot change
        // what is on screen - only renderChips() has anything to redo.
        const [dim, , grain] = key.split(':');
        if (parked[grain]) parked[grain] = parked[grain].filter((f) => f.key !== dim);
        renderChips();
        renderChipRow();
        return;
      } else if (key.endsWith(':ex')) {
        const f = findF(key.slice(0, -3));
        if (f) f.ex = [];
      } else {
        const f = findF(key);
        if (f) f.values = [];
      }
      tidyFilters();
      applyNow();
      return;
    }
    const edit = e.target.closest('[data-edit]');
    if (edit) {
      // util.js closes every menu on a click that is neither a [data-menu] trigger nor
      // inside an open menu - and a chip is both of those things' opposite, so without
      // this the popover would be opened and then immediately shut by the same click.
      e.stopPropagation();
      if (isMobile()) openFiltersSheet(edit.dataset.edit);
      else openFilterPop($('#addfpw'), edit.dataset.edit);
    }
  });

  /* The all/any control is joined to the box on desktop and inside the Searching-in
     sheet on a phone, so it is delegated on the document rather than bound to
     #wseg - neither live copy is inside it. */
  document.addEventListener('click', (e) => {
    const seg = e.target.closest('[data-wjoin]');
    if (!seg) return;
    // BEFORE the re-render, not after: picking from the joined control's [data-menu]
    // popover rewrites #wjoin-left, which takes the open menu's node out of the DOM
    // with it. util.js's closer walks `.menu.open`, so a node removed while open never
    // reaches it and the scroll lock it armed is never released.
    closeMenus();
    state.matchAll = seg.dataset.wjoin === 'all';
    applyNow();
  });

  /* The chip row (phone only). Tapping the chip of the sheet that is already open
     closes it, so a chip is never a no-op. */
  const MCHIP = {
    filters: () => openFiltersSheet(null),
    scope: () => openScopeSheet(),
    sort: () => openSortSheet(),
    saved: () => openSavedSheet(),
    clear: () => clearAll(),
  };
  $('#chiprow').addEventListener('click', (e) => {
    const chip = e.target.closest('[data-mchip]');
    if (!chip) return;
    const run = MCHIP[chip.dataset.mchip];
    if (!run) return;
    if (sheetEl && sheetKind === chip.dataset.mchip) { closeSheet(); return; }
    run();
  });

  /* The page-actions kebab. Rendered only when it would hold something, and only
     visible below the breakpoint - above it the two buttons are on the page. */
  const pageKebab = $('#page-kebab');
  if (pageKebab) pageKebab.addEventListener('click', () => openPageSheet());

  /* One implementation, two triggers: the toolbar's button above the breakpoint
     and the chip row's below it. */
  function clearAll() {
    state.q = '';
    state.filters = [];
    /* Parked chips go with them. Without this, "clear" leaves behind a filter that
       reappears the next time you flip the grain - the silent-return defect in its
       worst form, because nothing on screen ever showed it was still there. */
    clearParked();
    state.sortNote = null;
    qbox.value = '';
    $('#sb-clr').style.display = 'none';
    // Both of these offer to put back a search that has just been cleared on
    // purpose: the drill-in's way back, and the name of the saved search this no
    // longer is. (The saved search itself is untouched - only the claim that you
    // are looking at it.)
    searchSnapshot = null;
    loadedSaved = null;
    closeMenus();
    closeSugg();
    closeSheet();
    applyNow();
    // Said out loud because it is the one thing "Clear all" deliberately does NOT clear.
    showToast('Cleared the text and every filter. Search in is left alone - it is a scope, '
              + 'not a filter.');
  }
  $('#cs-clear').addEventListener('click', clearAll);

  // One delegated handler for the results region: rows are rebuilt on every
  // render, so nothing may hold a reference to a node inside it.
  $('#atable').addEventListener('click', (e) => {
    const sortEl = e.target.closest('[data-sort]');
    if (sortEl) { setSort(sortEl.dataset.sort); return; }
    // The card's kebab: every action that is about ONE channel.
    const kebab = e.target.closest('[data-rowkebab]');
    if (kebab) {
      // On the airing grain the sheet is about the SHOWING, so the airing id rides
      // along - the channel id alone could not tell three showings on one channel apart.
      openRowSheet(Number(kebab.dataset.rowkebab), Number(kebab.dataset.air) || null);
      return;
    }
    // A GROUP card's kebab. Its own sheet, not the channel one with rows disabled: the
    // subtraction is the design, and a sheet of greyed-out channel actions would be a
    // list of things this row cannot do rather than a list of what it can.
    const gkebab = e.target.closest('[data-groupkebab]');
    if (gkebab) { openGroupSheet(Number(gkebab.dataset.groupkebab)); return; }
    const act = e.target.closest('[data-act]');
    if (act) {
      const id = Number(act.dataset.id);
      /* The DUP badge drills straight in on a wide window, where its tooltip has
         already said what the cluster is. Touch has no tooltip, so down here the
         same badge opens the sheet that says it and offers the drill-in as a
         button (DESIGN.md 13.1). */
      /* No guide branch here any more: no ROW carries a guide control at this width
         (dev/changelog/860). The phone's kebab sheet still does, and it has its own
         handler - a branch kept here "in case" would be a second, unreachable copy of it. */
      if (act.dataset.act === 'dup-badge') { if (isMobile()) openDupSheet(id); else dupDrillIn(id); }
      /* A group row's only action is opening the group. Its guide toggle used to be here
         too, spelled two ways, and both did nothing but navigate: adding a group to the
         guide has an invariant behind it (a guide group must keep a recording-enabled
         member, `dev/changelog/757`) and removing one can cancel scheduled recordings, so
         neither was ever done from a search result. The whole affordance went with the
         channel row's "+ Add to Guide" (dev/changelog/860) - the group's page is where a
         guide row is decided, and this is what opens it - bindNavClicks below. */
      else if (REC_ACT_KEYS.has(act.dataset.act)) openRecordModal(Number(act.dataset.air), act);
    }
  });

  /* Every NAVIGATION out of the results region, answered once for the click and the
     middle button alike, so Ctrl/Cmd-click opens a new tab here the way it does on a real
     link (util.js::bindNavClicks, dev/changelog/996). */
  bindNavClicks($('#atable'), (e) => {
    if (e.target.closest('[data-sort], [data-rowkebab], [data-groupkebab]')) return null;
    const act = e.target.closest('[data-act]');
    if (act) {
      if (act.dataset.act === 'open-channel') return `${CFG.channelUrlBase}${Number(act.dataset.id)}`;
      if (act.dataset.act === 'open-group') return `${CFG.groupDetailUrlBase}${Number(act.dataset.id)}`;
      return null;
    }
    // The whole row opens the channel, which is what .arow's cursor promises -
    // but only where nothing else claimed the click, and never while text is
    // being selected out of it (util.js::hasSelectionIn).
    const row = e.target.closest('.arow');
    // Interactive things only: a cell that merely carries a tooltip is inert
    // text, and excluding those would make most of the row dead.
    if (!row || e.target.closest('a, button, input, label')) return null;
    if (hasSelectionIn(row)) return null;
    // A group row opens its group; everything else opens its channel.
    return row.dataset.group
      ? `${CFG.groupDetailUrlBase}${row.dataset.group}`
      : `${CFG.channelUrlBase}${row.dataset.id}`;
  });

  /* Checkboxes: the row ticks and the header's select-all, delegated for the
     same reason - #ch-list is rewritten on every render. */
  $('#atable').addEventListener('change', (e) => {
    if (e.target.closest('#all-select-all')) {
      const on = e.target.checked;
      pageChannels().forEach((c) => setRowSelected(c.id, on));
      updateSelectionUI();
      return;
    }
    const cb = e.target.closest('#ch-list input[type=checkbox]');
    if (!cb || !cb.dataset.id) return;
    setRowSelected(Number(cb.dataset.id), cb.checked);
    updateSelectionUI();
  });

  /* Select All is the PAGE, not the result set: it can only ever tick the rows
     that were fetched, and saying "Select All" while ticking 100 of 134,399
     would be the lie the count line exists to prevent. */
  $('#sel-all-btn').addEventListener('click', () => {
    const page = pageChannels();
    page.forEach((c) => setRowSelected(c.id, true));
    updateSelectionUI();
    if (last.total > last.rows.length) {
      showToast(`Selected the ${nf(page.length)} channel${page.length === 1 ? '' : 's'} on this `
                + `page. ${nf(last.total)} ${grainUi().noun}${last.total === 1 ? '' : 's'} match `
                + 'this search - page through to add more.');
    }
  });
  $('#desel-all-btn').addEventListener('click', () => {
    selected.clear();
    updateSelectionUI();
  });

  /* ── The Fields sheet: what each card shows ──────────────────────────
     The phone's answer to the Columns popover, and deliberately a smaller
     control - see `cardFields` above. It is on the list header, which is the one
     place it cannot be mistaken for a search control. */
  const fieldsSheetBody = () => colList().map((c) => `<label class="prow">
      <span class="pv">${esc(c.label)}</span>
      <label class="switch"><input type="checkbox" data-cardfield="${esc(c.key)}"${
        cardFields().has(c.key) ? ' checked' : ''}><span class="knob"></span></label>
    </label>`).join('')
    + `<div class="sh-note">The badge line is not in this list and cannot be switched off. It
       carries In Guide, the "in guide via a group" badge, the DUP and KEPT badges, the lifecycle
       pair (New / Missing), Not normalized and the "why this matched" chip - the duplicate story,
       how a channel with no guide row of its own is still in your guide, what the last sync did to
       this channel, and the answer to why the row is in front of you at all.</div>
     <div class="sh-note">The ${esc(grainUi().pinLabel.toLowerCase())} is always shown and never
       truncated. This is kept separately from the desktop column setup and separately per tab, so
       a phone and a desktop - and Channels and Guide - can each want different things; all of
       them follow you across browsers.</div>`;

  const fieldsSheetTitle = () =>
    `Shown on each card (${cardFields().size} of ${colList().length})`;

  function openFieldsSheet() {
    const redraw = () => {
      setSheetTitle(fieldsSheetTitle());
      sheetBody().innerHTML = fieldsSheetBody();
    };
    const sheet = openSheet({
      kind: 'fields', title: fieldsSheetTitle(), body: fieldsSheetBody(), redraw,
    });
    /* Re-rendered from `cardFields` rather than from the checkbox that fired, so
       the switches and the cards have one source of truth. Visibility only - it
       changes what is DRAWN and nothing about the query, so the rows already in
       hand are redrawn and the search is not re-run. */
    sheet.addEventListener('change', (e) => {
      const cb = e.target.closest('[data-cardfield]');
      if (!cb) return;
      if (cb.checked) cardFields().add(cb.dataset.cardfield);
      else cardFields().delete(cb.dataset.cardfield);
      saveCardFields();
      renderResults();
      redraw();
    });
  }

  /* ── The columns picker ──────────────────────────────────────────────
     util.js keeps a menu open for clicks on content that is not a link or a
     .menu-item, which is exactly what these checkboxes are - so no
     stopPropagation is needed here, and the list is deliberately NOT rebuilt on
     a tick (rebuilding would detach the node mid-click; the drop handler that
     does rebuild runs on a drag event, not a click). */
  $('#cols-list').addEventListener('change', (e) => {
    const cb = e.target.closest('input[data-col]');
    if (cb) setColumnVisible(cb.dataset.col, cb.checked);
  });
  let dragKey = null;
  $('#cols-list').addEventListener('dragstart', (e) => {
    const item = e.target.closest('.col-item');
    if (item) dragKey = item.dataset.col;
  });
  $('#cols-list').addEventListener('dragover', (e) => {
    const item = e.target.closest('.col-item');
    if (!item || !dragKey) return;
    e.preventDefault();
    $$('#cols-list .col-item').forEach((x) => x.classList.remove('drag-over'));
    item.classList.add('drag-over');
  });
  $('#cols-list').addEventListener('drop', (e) => {
    const item = e.target.closest('.col-item');
    if (!item || !dragKey) return;
    e.preventDefault();
    const cs = colState();
    const order = cs.order.filter((k) => k !== dragKey);
    order.splice(order.indexOf(item.dataset.col), 0, dragKey);
    cs.order = order;
    dragKey = null;
    renderColsPop();
    saveColumns();
    applyColumns();
    renderResults();
  });

  /* ── The selection bar ───────────────────────────────────────────────
     Every action here is one that already ships: the shared check modal, the
     guide add endpoint and the shared group modal. */
  /* The ad hoc test of ONE channel, from the phone's row sheet. It is the only caller
     left: the bulk "Test selected" that used to sit on the selection bar is gone, merged
     into the group-create flow (dev/changelog/831). A single channel has no group to
     derive a name from, so this keeps the named ad hoc path - the same one
     channel-detail.js's "Test this channel" uses. */
  function testChannels(ids) {
    if (!ids.length) { showToast('Select some channels first.', { type: 'error' }); return; }
    const now = new Date();
    const pad = (v) => String(v).padStart(2, '0');
    const name = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} `
      + `${pad(now.getHours())}:${pad(now.getMinutes())}${state.q ? ` - ${state.q}` : ''}`;
    openCreateCheckModal({
      channelIds: ids,
      memberCount: ids.length,
      modalTitle: ids.length === 1
        ? 'Test this channel' : `Test ${nf(ids.length)} selected channels`,
      defaultName: name,
      profiles: CFG.checkProfiles,
      profilesUrl: CFG.checkProfilesUrl,
      testerBusy: CFG.testerBusy,
      scheduleTemplateId: 'cc-schedule-fields',
      schedulePrefix: 'browsesched',
      allowAttachExisting: true,
      // check-modal.js already toasts the accurate outcome itself - "created" for a new
      // check, "N channel(s) added" for an existing one - before calling onDone(). This
      // page doesn't reload (unlike most other openCreateCheckModal callers), so onDone
      // must stay a no-op rather than fall through to the default reload, and must not
      // toast again itself or it stomps whichever message check-modal.js just showed.
      onDone: () => {},
    });
  }

  /* One request per channel, exactly as today's bulk add does - the endpoint is
     per-channel and it is the endpoint that knows about the duplicate warning.
     The warnings are collected and asked about ONCE, because a prompt per
     channel across a 40-row selection is not a question, it is an obstacle. */
  async function addSelectionToGuide(btn, ids) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Adding…';
    try {
      const results = await Promise.all(
        ids.map((id) => jsonFetch(`${CFG.addGuideUrlBase}${id}/add`, { method: 'POST' })));
      let added = results.filter((d) => d.success && !d.duplicate_warning).length;
      const warned = results.filter((d) => d.duplicate_warning);
      if (warned.length) {
        const lines = warned.map((w) => `${w.channel_name} shares a stream URL with: `
          + (w.conflicting_channels || []).map((c) => c.name).join(', '));
        const proceed = window.confirm(
          `${warned.length} channel${warned.length === 1 ? '' : 's'} share a stream URL with a `
          + `channel already in your guide:\n\n${lines.join('\n')}\n\nAdd them anyway?`);
        if (proceed) {
          const forced = await Promise.all(warned.map((w) =>
            jsonFetch(`${CFG.addGuideUrlBase}${w.channel_id}/add?force=1`, { method: 'POST' })));
          added += forced.filter((d) => d.success).length;
        }
      }
      showToast(`${nf(added)} channel${added === 1 ? '' : 's'} added to your guide.`);
      applyNow(false);
    } catch (err) {
      showToast(err.message || 'Could not add every channel. Try again.', { type: 'error' });
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  /* One request for the whole selection, unlike adding: removing a guide row asks no
     per-channel question, so there is nothing a request per channel would buy - and one
     request is one transaction and one hide recompute. The server no-ops any channel that
     is already out of the guide, so a stale row payload here cannot fail the request. */
  async function removeSelectionFromGuide(btn, ids) {
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Removing…';
    try {
      const data = await jsonFetch(CFG.removeGuideUrl, {
        method: 'POST', body: JSON.stringify({ channel_ids: ids }),
      });
      const n = (data.removed || []).length;
      showToast(`${nf(n)} channel${n === 1 ? '' : 's'} removed from your guide.`);
      // The selection is deliberately KEPT - the same rows are still ticked and the button
      // has flipped back to Add, so undoing an accidental bulk remove is one click.
      applyNow(false);
    } catch (err) {
      showToast(err.message || 'Could not remove every channel. Try again.', { type: 'error' });
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  $('#sel-add').addEventListener('click', (e) => {
    const btn = e.currentTarget;
    const ids = Array.from(selected.keys());
    if (!ids.length) { showToast('Select some channels first.', { type: 'error' }); return; }
    // The mixed case never reaches here - the button is disabled - but the mode decides
    // which action this is, so it is read rather than inferred from the label.
    if (guideActionMode() === 'remove') removeSelectionFromGuide(btn, ids);
    else addSelectionToGuide(btn, ids);
  });

  /* Hide, and un-hide, the selection. One endpoint and one tri-state `override`, so the
     two buttons cannot come to mean subtly different things - un-hide posts null ("follow
     the rules"), not false, because undoing a hide is not the same as asserting that no
     rule may ever hide this channel.

     Protection defers rather than refuses, so a selection of 100 with 1 in the guide hides
     99 and the response names the 1. That is deliberate, and it is why this reports two
     numbers instead of succeeding silently. */
  async function hideSelection(btn, ids, override) {
    if (!ids.length) { showToast('Select some channels first.', { type: 'error' }); return; }
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = override === true ? 'Hiding…' : 'Un-hiding…';
    try {
      const data = await jsonFetch(CFG.hideUrl, {
        method: 'POST', body: JSON.stringify({ channel_ids: ids, override }),
      });
      const deferred = data.deferred || [];
      if (override === true) {
        let msg = `${nf(data.hidden)} channel${data.hidden === 1 ? '' : 's'} hidden.`;
        if (deferred.length) {
          const names = deferred.slice(0, 3).map((c) => c.name).join(', ');
          const more = deferred.length > 3 ? ` and ${nf(deferred.length - 3)} more` : '';
          msg += ` ${nf(deferred.length)} kept visible for now - ${names}${more}`
            + ` ${deferred.length === 1 ? 'is' : 'are'} in the TV Guide or a channel group.`;
        }
        showToast(msg);
      } else {
        /* The gap an un-hide leaves: hiding deleted these channels' EPG entries and this
           does not fetch them back, so their guide rows stay empty until each account's
           next sync. Naming it is the whole point - an empty guide row otherwise reads as
           a broken channel. */
        let msg = `${nf(data.matched)} channel${data.matched === 1 ? '' : 's'} are visible again.`;
        if (data.epg_gap) {
          msg += ` ${nf(data.epg_gap)} of them have no guide data until their account next syncs.`;
        }
        showToast(msg);
      }
      selected.clear();
      applyNow(false);
    } catch (err) {
      showToast(err.message || 'Could not change every channel. Try again.', { type: 'error' });
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
  $('#sel-hide').addEventListener('click', (e) =>
    hideSelection(e.currentTarget, Array.from(selected.keys()), true));
  $('#sel-unhide').addEventListener('click', (e) =>
    hideSelection(e.currentTarget, selectedHiddenIds(), null));

  /* The bar's one primary action, and the only one that groups. It opens the three-screen
     create flow (group-create-flow.js), which ends in a single Create that writes the
     group and its health check together - a group with no test history has nothing for
     any format strategy to choose from, so "run a check first" is the only honest answer
     to every question a create screen could ask (dev/changelog/831). */
  $('#sel-group').addEventListener('click', () => {
    if (!selected.size) { showToast('Select some channels first.', { type: 'error' }); return; }
    openGroupCreateFlow({
      channels: selectedChannels(),
      introSeenVersion: CFG.groupIntroSeenVersion,
      groupDetailUrlBase: CFG.groupDetailUrlBase,
      checkOpts: {
        profiles: CFG.checkProfiles,
        profilesUrl: CFG.checkProfilesUrl,
        testerBusy: CFG.testerBusy,
        windowSettingsUrl: CFG.windowSettingsUrl,
        scheduleTemplateId: 'cc-schedule-fields',
        schedulePrefix: 'browsesched',
      },
    });
  });

  /* Delete the missing channels out of the current selection, through the same shared
     preview-then-delete modal the whole-database sweep above uses (dev/changelog/772).
     The ids narrow WHICH channels are considered; the server still re-derives whether
     each is missing and whether anything blocks it, so a stale row payload here cannot
     talk it into deleting a live channel. */
  $('#sel-delete').addEventListener('click', () => {
    const ids = selectedMissingIds();
    if (!ids.length) { showToast('None of the selected channels are missing.', { type: 'error' }); return; }
    openMissingModal({
      title: 'Delete Missing Channels',
      scopeText: 'from your selection',
      emptyText: 'None of the selected channels can be deleted right now.',
      previewUrl: `${CFG.missingPreviewUrl}?channel_ids=${ids.join(',')}`,
      deleteUrl: CFG.missingDeleteUrl,
      deleteBody: { channel_ids: ids },
      /* A full reload, the same as the whole-database sweep - not a refetch keeping the
         surviving selection. Rows, facet counts and the header's own "Delete Missing
         Channels (N)" are all now describing channels that no longer exist, and that
         header count is server-rendered so no client refetch reaches it. Losing the rest
         of the selection is the smaller cost of the two. */
      onDone: () => { location.reload(); },
    });
  });

  /* The action context: "you came here to add channels to group X". It is not a
     filter - it narrows nothing - so it is never a chip; it stays until it is
     dismissed, and dismissing it takes add_to_group out of the URL
     (DESIGN-channel-search.md 3). */
  $('#sel-ctx-add').addEventListener('click', () => {
    openGroupModal({
      channels: selectedChannels(),
      fixedGroup: { id: state.addToGroup, name: addToGroupName() },
      onDone: () => { location.href = `${CFG.groupDetailUrlBase}${state.addToGroup}`; },  // nav-ok: redirect after adding to the group
    });
  });
  $('#sel-ctx-x').addEventListener('click', () => {
    state.addToGroup = null;
    updateSelectionUI();
    applyNow(false);
    showToast('Dropped the "adding to a group" context. The search itself is unchanged.');
  });

  /* The second action context, dismissed the same way: the parameter leaves the URL and
     every Replace button goes back to reading Record. The scheduled recording is not
     touched - dismissing a context is not an action on the thing it named. */
  $('#replace-x').addEventListener('click', () => {
    state.replaceRec = null;
    renderReplaceBar();
    renderResultsArea();
    applyNow(false);
    showToast('Dropped the "replacing a recording" context. '
              + 'The scheduled recording and this search are both unchanged.');
  });

  /* One delegated handler for the whole rail: it is rewritten on every render,
     so nothing may hold a reference to a control inside it. Order matters - the
     create button and the value controls sit INSIDE the header's and the row's
     own click targets, so they are tested first or clicking them would just
     collapse the facet. */
  $('#rail').addEventListener('click', (e) => {
    const make = e.target.closest('[data-make]');
    if (make) { DIM_CREATE[make.dataset.make].run(); return; }

    const tri = e.target.closest('.tri button[data-key]');
    if (tri) { toggleValue(tri.dataset.key, tri.dataset.value, tri.dataset.dir === 'exc'); return; }

    const more = e.target.closest('[data-more]');
    if (more) {
      const key = more.dataset.more;
      if (railShowAll.has(key)) railShowAll.delete(key); else railShowAll.add(key);
      renderRail();
      return;
    }
    const clr = e.target.closest('[data-findclr]');
    if (clr) { railQ[clr.dataset.findclr] = ''; renderRail(); return; }

    const head = e.target.closest('[data-fach]');
    if (head) {
      const key = head.dataset.fach;
      if (railOpen.has(key)) railOpen.delete(key); else railOpen.add(key);
      renderRail();
      return;
    }
    // A click on the row (but not on its +/-) includes the value: the whole row
    // is the affordance for the common direction.
    const row = e.target.closest('.prow[data-key]');
    if (row) { toggleValue(row.dataset.key, row.dataset.value, false); return; }

    const st = e.target.closest('[data-standing]');
    if (st) { toggleStanding(st.dataset.standing); return; }
    if (e.target.closest('#st-reset')) { resetStanding(); return; }
    if (e.target.closest('#rail-clr')) { state.filters = []; applyNow(); return; }
    if (e.target.closest('#rail-toggle')) {
      railCollapsed = !railCollapsed;
      renderRail();
      // The table's width changed, so the frozen name track is stale.
      freezeNameCol();
    }
  });

  $('#rail').addEventListener('input', (e) => {
    const box = e.target.closest('[data-railq]');
    if (!box) return;
    railQ[box.dataset.railq] = box.value;
    renderRail();
  });

  // The empty state's "widen it" hint, which is a control only at phone width.
  $('#all-empty').addEventListener('click', (e) => {
    if (e.target.closest('#es-scope')) openScopeSheet();
  });

  // Changes this search only - the stored default is Settings' alone. The address bar
  // picks the new size up from the response's query_string like every other change.
  $('#pager').addEventListener('change', (e) => {
    if (e.target.id !== 'pg-size') return;
    const size = parseInt(e.target.value, 10);
    if (!size || size === state.pageSize) return;
    state.pageSize = size;
    applyNow();
  });

  $('#pager').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pg]');
    if (!btn || btn.disabled) return;
    state.page += btn.dataset.pg === 'next' ? 1 : -1;
    applyNow(false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  $('#cs-count').addEventListener('click', (e) => {
    const off = e.target.closest('[data-standing-off]');
    // toggleStanding, not `delete`: this entry only renders for an option that is REMOVING
    // rows, and for a `show*` key that is the state where the key is already absent, so
    // deleting it is a no-op and the click does nothing (dev/changelog/778). Flipping
    // membership stops the hiding whichever way the key reads.
    if (off) { toggleStanding(off.dataset.standingOff); return; }
    if (e.target.closest('#cs-scope')) {
      if (isMobile()) openScopeSheet(); else $('#cs-q').focus();
      return;
    }
    if (e.target.closest('#cs-back')) {
      const snap = searchSnapshot;
      searchSnapshot = null;
      restore(snap);
      showToast('Restored your search from before the duplicate drill-in.');
    }
  });

  /* ── The two whole-database cleanups above the list ──────────────────
     Both are ported unchanged from today's Browse tab, including the scope
     distinction that catches people out: Review Duplicates is GUIDE-scoped
     (it resolves channels already in your guide that share a URL), while the
     DUP badge on a row is account-global. */
  function openReviewDupsModal() {
    openDupModal({
      groups: CFG.dupGroups,
      showGuideChoice: false,
      introHtml: 'The channels you don\'t keep are removed from <strong>your TV Guide</strong>. '
        + 'They are not deleted - they stay in your channel list and can be added back at any time.',
      onSubmit: (removals, transfer) => jsonFetch(CFG.removeDupsUrl, {
        method: 'POST',
        body: JSON.stringify({ removals, transfer }),
      }).then(() => { location.reload(); return { ok: true }; })
        .catch((err) => ({ error: err.message || 'Request failed.' })),
    });
  }
  const reviewDupBtn = $('#review-dup-btn');
  if (reviewDupBtn) reviewDupBtn.addEventListener('click', openReviewDupsModal);

  /* Whole-database, not account-scoped: this page has no account selected until
     the user picks one as an ordinary filter, and the button names a cleanup of
     everything. Shared with the group/health-check detail page's scoped version -
     static/js/missing-modal.js. */
  function openMissingDeleteModal() {
    openMissingModal({
      title: 'Delete Missing Channels',
      scopeText: 'across all accounts',
      previewUrl: CFG.missingPreviewUrl,
      deleteUrl: CFG.missingDeleteUrl,
      deleteBody: { account_id: null },
      onDone: () => location.reload(),
    });
  }

  const missingBtn = $('#missing-btn');
  if (missingBtn) missingBtn.addEventListener('click', () => openMissingDeleteModal());

  /* The phone's page-actions sheet: the same two cleanups, behind the header
     kebab rather than as two danger buttons above the search box. Each one opens
     a modal of its own, so the sheet is closed first - two stacked overlays is
     exactly what "one open at a time" exists to prevent. */
  function openPageSheet() {
    const rows = [];
    if (missingBtn) {
      rows.push(`<div class="prow drill" data-page-act="missing"><span class="pico">&#128465;</span>
        <span class="pv">${esc(missingBtn.textContent.trim())}</span></div>`);
    }
    if (reviewDupBtn) {
      rows.push(`<div class="prow drill" data-page-act="dups"><span class="pico">&#128465;</span>
        <span class="pv">${esc(reviewDupBtn.textContent.trim())}</span></div>`);
    }
    const sheet = openSheet({
      kind: 'page',
      title: 'Channel list actions',
      body: rows.join('') + '<div class="sh-note">Both act on the whole database rather than on '
        + 'this search. Review Duplicates is guide-scoped - it resolves channels already in your '
        + 'guide that share a stream URL, which is a different question from the DUP badge a row '
        + 'carries.</div>',
    });
    sheet.addEventListener('click', (e) => {
      const act = e.target.closest('[data-page-act]');
      if (!act) return;
      const which = act.dataset.pageAct;
      closeSheet();
      if (which === 'missing') openMissingDeleteModal(); else openReviewDupsModal();
    });
  }

  // The frozen name track is a measurement, so it has to be retaken whenever the
  // space it was measured in changes. The bar's sticky offset does not need a call
  // here any more - the ResizeObserver set up alongside syncBarOffset() above already
  // fires on any box-size change to .cs-bar, a window resize included.
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { freezeNameCol(); }, 120);
  });

  // Sidebar-width overflow (dev/docs/BUGS.md): #ch-list is its own scroll box (see
  // the .atable comment in channel-search.css for why #ahead can't be), so its
  // horizontal position is mirrored onto the sticky header to keep columns aligned.
  // #ch-list itself is never replaced (only its row children are, per render), so
  // one listener attached at load covers every render.
  const chListEl = $('#ch-list');
  if (chListEl) {
    chListEl.addEventListener('scroll', () => {
      const head = $('#ahead');
      if (head) head.scrollLeft = chListEl.scrollLeft;
    });
  }

  /* Crossing the breakpoint - a rotate, or a desktop window being dragged narrow -
     changes which of two completely different renderings the same state produces,
     so everything above the results and the results themselves are redrawn. A
     sheet is closed rather than carried across: it has no meaning on the side that
     has a rail. */
  const onBreakpointChange = () => {
    closeSheet();
    railCollapsed = false;
    renderRail();
    renderBar();
    renderResults();
  };

  /* The grain toggle, delegated on the document because the same builder fills two
     slots and only one of them exists at a given width. */
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('[data-grain]');
    if (!tab) return;
    setGrain(tab.dataset.grain);
  });
  if (MOBILE_MQ.addEventListener) MOBILE_MQ.addEventListener('change', onBreakpointChange);
  else if (MOBILE_MQ.addListener) MOBILE_MQ.addListener(onBreakpointChange);

  /* ── Boot ────────────────────────────────────────────────────────────
     The catalog first, because the state cannot be parsed without the
     registries it validates against (which fields exist, which dimensions, what
     the defaults are). One request, once per page load. */
  jsonFetch(CFG.catalogUrl)
    .then((data) => {
      CAT = data;
      /* One loader for both grains' stored setups, because the merge rules are the
         same on both sides and writing them twice is how the two drift. Unknown keys
         are dropped and new columns appended, so a setup stored before a column
         existed still opens; and a stored CARD set REPLACES the defaults rather than
         merging, because "I turned that one off" has to survive a reload. */
      const loadPrefs = (grain, reg, colPrefs, fieldPrefs) => {
        const cs = colStates[grain];
        if (colPrefs && Array.isArray(colPrefs.order)) {
          const known = colPrefs.order.filter((k) => reg[k]);
          cs.order = known.concat(cs.order.filter((k) => !known.includes(k)));
          cs.hidden = (colPrefs.hidden || []).filter((k) => reg[k]);
        }
        if (Array.isArray(fieldPrefs)) {
          const set = cardFieldSets[grain];
          set.clear();
          fieldPrefs.filter((k) => reg[k]).forEach((k) => set.add(k));
        }
      };
      loadPrefs(GRAIN_CHANNELS, COL_BY_KEY, CFG.columnPrefs, CFG.cardFieldPrefs);
      loadPrefs(GRAIN_AIRINGS, ACOL_BY_KEY, CFG.airingColumnPrefs, CFG.airingCardFieldPrefs);
      parseState(location.search);
      // The `when`/`duration` controls are spelled INTO one filter value each, so they have
      // to be read back out of whatever the URL carried or a reload would show the chip over
      // empty boxes - a filter you can see but cannot edit.
      hydrateWhenControls();
      hydrateDurationControls();
      $('#cs-q').value = state.q;
      applyColumns();
      renderColsPop();
      /* Pending is still true here, so this paints the loading state, the grain tab
         strip and the column header rather than leaving the results card blank until
         the first response - which on the airing grain is seconds. It also covers what
         used to be a bare updateSelectionUI() call at this point: the action context
         arrives in the URL, so the bar that names it has to be up before the first
         result lands, and renderResultsArea() ends by calling it. The AREA only,
         because applyNow() on the next line draws the rail and the bar anyway. */
      renderResultsArea();
      applyNow(false);
    })
    .catch((e) => {
      showToast(`Could not load the search catalog: ${e.message || 'request failed'}`,
                { type: 'error' });
    });
})();
