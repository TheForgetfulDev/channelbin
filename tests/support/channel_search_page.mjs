/* Drives the REAL /channels page in jsdom and prints what it observed as JSON.
   Called by tests/test_channel_search_page_js.py, which owns every assertion - this
   file only reports, so a failure reads as "the page did X" in Python rather than as a
   node exit code.

   Why it exists: the page's whole behaviour is client side, and the server-rendered
   markup IS the "nothing active" state, so a page whose JS never ran looks exactly like
   an empty one to curl and to the JSON endpoint tests. That is not hypothetical - the
   module's config global was unreachable for three whole steps of the build and nothing
   went red (dev/docs/BUGS.md 2026-07-30 05:15 PM).

   What is real here and what is not: the markup is the page the Flask route actually
   rendered, the catalog and the search responses are what the real endpoints answered
   over seeded rows, and util.js + channel-search.js are the shipped files, evaluated
   into the window the way their <script src> would have. Only the network is faked, and
   every mutating request is RECORDED rather than sent. jsdom computes no layout, so
   nothing geometric can be asked here - the frozen Channel column width and the three
   shared modals (<script src> jsdom does not fetch) need a browser.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html, catalog.json and
   three real search responses: rows.json, rows_dup.json (nothing hidden, so a whole
   duplicate cluster is present) and rows_paged.json (asked for one row a page, so the
   pager has a Next). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGE = read('page.html');
const CATALOG = JSON.parse(read('catalog.json'));
const ROWS = JSON.parse(read('rows.json'));
const ROWS_DUP = JSON.parse(read('rows_dup.json'));
const ROWS_PAGED = JSON.parse(read('rows_paged.json'));
/* The airing grain's fixture, reassembled from the three requests the page really makes.
   Its row response declines the total and the rail - with `past` off every aggregate reads
   the whole `epg_entries` table, so the server hands back the rows and nothing else
   (dev/changelog/681) - and the page then fills both in from their own endpoints. Merging
   them here is what lets a scenario about the airing grain's RENDERING start from the
   settled state instead of from the one intermediate frame. `declined` is cleared for the
   same reason: a scenario that wants a decline asks for one with `declineWhy`. */
const ROWS_AIRINGS = Object.assign(
  JSON.parse(read('rows_airings.json')),
  JSON.parse(read('airings_counts.json')),
  JSON.parse(read('airings_facets.json')),
  { declined: [], declined_reason: '' });
const ROWS_ACCTFACETS = JSON.parse(read('rows_acctfacets.json'));

const JS = ['util.js', 'channel-search.js'].map(
  (f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

/* One booted page. `rows` picks which search response every /api/channels/search
   request is answered with; `saved` seeds the saved-search list the route would have
   rendered into the config object; `width` is the viewport the page believes it is in,
   which is what selects the desktop table or the phone cards.

   `hold` is a GRAIN NAME, and it parks every /api/channels/search response for that
   grain until c.release() (or c.rejectHeld()) is called. Nothing about the page's
   in-flight state is otherwise reachable here: the stub answers synchronously, so the
   loading state would be up and down again inside one microtask.

   `holdCounts` parks the airing grain's trailing GET /api/channels/search/counts request
   (dev/changelog/598) alone - separate from `hold`, which also catches the ROW request
   for a grain and would leave rows never landing at all. This is what makes "rows are up,
   counts are still pending" an observable state rather than something that only ever
   exists for one microtask.

   `declineWhy` makes every response decline its numbers and give that string as the
   reason, which is what the server does when the aggregates would cost seconds - the index
   is unusable, or it cannot answer the search at all (dev/changelog/676, 681). The page
   must render the server's sentence rather than one of its own. */
function boot({ rows = ROWS, saved = null, url = 'http://localhost:5000/channels',
                width = 1280, hold = null, holdCounts = false, declineWhy = null,
                pinnedWidth = null, catalog = CATALOG } = {}) {
  const held = [];
  // Mutable, so a scenario can boot normally and start parking only once it is about
  // to do the thing it cares about - the grain flip needs channel rows on screen first.
  let holdGrain = hold;
  let holdCountsFlag = holdCounts;
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const sent = [];
  const fetchStub = (target, opts = {}) => {
    const href = String(target);
    const method = (opts.method || 'GET').toUpperCase();
    let body = null;
    try { body = opts.body ? JSON.parse(opts.body) : null; } catch (e) { body = opts.body; }
    /* The SIGNAL is recorded per request, because "was the superseded one cancelled" is
       not answerable from the URL list - two identical requests differ only in whether
       one of them was called off (dev/changelog/417). */
    const entry = { url: href, method, body, signal: opts.signal || null, aborted: false };
    sent.push(entry);
    let payload = { success: true };
    let park = false;
    if (href.includes('/record-context')) {
      const air = (ROWS_AIRINGS.rows || [])[0] || {};
      payload = {
        success: true,
        program: { id: air.id, title: air.title, start_time: air.start_time,
                   stop_time: air.stop_time, stream_url: 'http://example.test/live/u/p/1',
                   suggested_name: air.suggested_name, channel_id: (air.channel || {}).id,
                   group_id: null, has_recording: false, description: '' },
        channel: { id: (air.channel || {}).id, default_profile_id: null },
      };
    } else if (href.includes('/api/guide/channels/remove')) {
      // Echoed back rather than left as the bare `{success: true}` default, so the toast
      // the page raises is the one it would raise against the real route.
      payload = { success: true, removed: (body || {}).channel_ids || [] };
    } else if (href.includes('/api/channels/search/counts')) {
      // The trailing request fetchRows() no longer waits on for the airing grain
      // (dev/changelog/598) - total/pages/standing_hidden alone, echoed from the same
      // fixture the bundled endpoint would have used, so the two are never allowed to
      // disagree in a scenario. Checked BEFORE the generic `/api/channels/search` branch
      // below, since this URL is also a substring match for that one.
      const qs = new URLSearchParams(href.split('?')[1] || '');
      const empty = (qs.get('q') || '').startsWith('zzz-');
      const base = qs.get('grain') === 'airings' ? ROWS_AIRINGS : rows;
      payload = declineWhy
        ? { success: true, total: null, pages: null, standing_hidden: null,
            declined: ['counts'], declined_reason: declineWhy }
        : empty
          ? { success: true, total: 0, pages: 1, standing_hidden: {} }
          : { success: true, total: base.total, pages: base.pages,
              standing_hidden: base.standing_hidden };
      park = holdCountsFlag;
    } else if (href.includes('/api/channels/search/facets')) {
      /* The rail's own endpoint (dev/changelog/676). It used to be the row endpoint with
         `facets=<dims>&counts=0` on it, which ran the whole page query and threw the rows
         away. Checked BEFORE the generic `/api/channels/search` branch below for the same
         reason `/counts` is: this URL is a substring match for that one too. */
      const qs = new URLSearchParams(href.split('?')[1] || '');
      const base = qs.get('grain') === 'airings' ? ROWS_AIRINGS : rows;
      const asked = qs.getAll('facets').filter(Boolean);
      const facets = {};
      asked.forEach((k) => { if ((base.facets || {})[k]) facets[k] = base.facets[k]; });
      payload = declineWhy
        ? { success: true, facets: {}, facets_counted: [], declined: ['facets'],
            declined_reason: declineWhy }
        : { success: true, facets, facets_counted: Object.keys(facets).sort(),
            declined: [], declined_reason: '' };
    } else if (href.includes('/catalog')) payload = catalog;
    else if (href.includes('/api/channels/search')) {
      // Echo the request back as `query_string`, the way the engine does. Replaying one
      // frozen string instead would make every address-bar observation meaningless.
      const qs = new URLSearchParams(href.split('?')[1] || '');
      // The engine re-renders `query_string` from its own SearchState, so a parameter that
      // describes the REQUEST rather than the search never comes back. Dropped here for the
      // same reason - `sid`/`seq` are the page's own request bookkeeping
      // (dev/changelog/678), and echoing them would put a dead page load's id into a shared
      // link.
      qs.delete('facets');
      qs.delete('counts');
      qs.delete('sid');
      qs.delete('seq');
      // A `zzz-` query is the scenarios' spelling of "this matches nothing" - a search
      // whose results are the SAME rows cannot show whether a selection survived it.
      const empty = (qs.get('q') || '').startsWith('zzz-');
      // Answered per GRAIN, because the page asks ONE endpoint for both and a stub that
      // always returned channel rows would let an airing scenario pass against them.
      const base = qs.get('grain') === 'airings' ? ROWS_AIRINGS : rows;
      // The airing grain's row request asks to skip the breakdown (`counts=0`,
      // dev/changelog/598) - echoed here as a real `null`, matching what the real
      // endpoint sends back, so a scenario cannot accidentally pass against a total it
      // was never actually given synchronously.
      const skippedCounts = href.includes('counts=0');
      payload = Object.assign({}, base,
        empty ? { rows: [], total: 0, pages: 1 } : {},
        skippedCounts ? { total: null, pages: null, standing_hidden: null } : {},
        // Rows first, always: the row endpoint drops the numbers itself when they would be
        // expensive and reports which ones (dev/changelog/676, 681). Rows are untouched.
        declineWhy
          ? { total: null, pages: null, standing_hidden: null, facets: {},
              facets_counted: [], declined: ['counts', 'facets'],
              declined_reason: declineWhy }
          : {},
        { query_string: qs.toString() });
      // The channel grain is the one the URL leaves unspelled, so `hold: 'channels'`
      // has to match a request that carries no grain at all.
      park = holdGrain !== null && (qs.get('grain') || 'channels') === holdGrain;
    }
    const answer = () => ({
      ok: true, status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
    /* A parked request behaves like a real in-flight one: aborting it REJECTS with an
       AbortError rather than leaving the promise pending forever. Without that the
       page's abort catch path is never entered here, and "an abort raises no toast"
       would assert over code that never ran. */
    const armAbort = (reject) => {
      const sig = opts.signal;
      if (!sig) return;
      const onAbort = () => {
        entry.aborted = true;
        if (reject) reject(sig.reason || new Error('AbortError'));
      };
      if (sig.aborted) onAbort();
      else sig.addEventListener('abort', onAbort);
    };
    if (!park) { armAbort(null); return Promise.resolve(answer()); }
    return new Promise((resolve, reject) => {
      armAbort(reject);
      held.push({ resolve, reject, answer });
    });
  };

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',       // inline scripts run; external src= are not fetched
    pretendToBeVisual: true,
    url,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      /* jsdom implements NO matchMedia at all, and the page's one spelling of the
         768px breakpoint is matchMedia - so this stub is the only thing that makes
         375 drivable here. It answers from `width` rather than always false, which
         is what a bare jsdom would do and would silently test the desktop
         arrangement twice. Layout is still not computed: this decides which
         RENDERING runs, never how anything measures. */
      w.matchMedia = (query) => {
        const max = /max-width:\s*(\d+)px/.exec(query);
        const min = /min-width:\s*(\d+)px/.exec(query);
        const matches = (max ? width <= Number(max[1]) : true)
          && (min ? width >= Number(min[1]) : true);
        return {
          media: query, matches, onchange: null,
          addEventListener() {}, removeEventListener() {},
          addListener() {}, removeListener() {}, dispatchEvent() { return false; },
        };
      };
      Object.defineProperty(w, 'innerWidth', { value: width, configurable: true });
      /* jsdom computes no layout, so scrollWidth is 0 on every element and
         freezeNameCol() declines to freeze a meaningless number - which is the right
         production behavior and also why the pinned column was untestable here. A
         scenario that is ABOUT the freeze hands in the one measurement it needs, and
         only that one: everything else keeps answering 0, so nothing else in the page
         starts believing it has a layout engine. See BUGS.md 2026-08-20 09:12, where
         the freeze was measuring the resolved track instead of the cell and pinned the
         column at its own min-width forever. */
      if (pinnedWidth !== null) {
        Object.defineProperty(w.Element.prototype, 'scrollWidth', {
          configurable: true,
          get() { return this.classList.contains('a-namecell') ? pinnedWidth : 0; },
        });
      }
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  const toasts = [];
  if (saved) window.CHANNEL_SEARCH_CONFIG.savedSearches = saved;
  JS.forEach((src) => window.eval(src));
  // showToast is util.js's, and it is what the page uses to say out loud that it did
  // something the user did not ask for. Wrapped after load so the real one still runs.
  const realToast = window.showToast;
  window.showToast = (msg, opts) => { toasts.push(String(msg)); return realToast(msg, opts); };

  const ctx = {
    window, document, sent, toasts, errors,
    $: (s) => document.querySelector(s),
    $$: (s) => Array.from(document.querySelectorAll(s)),
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    change: (el) => el.dispatchEvent(new window.Event('change', { bubbles: true })),
    type: (el, v) => { el.value = v; el.dispatchEvent(new window.Event('input', { bubbles: true })); },
    focus: (el) => el.dispatchEvent(new window.FocusEvent('focus', { bubbles: false })),
    wait: (ms) => new Promise((r) => setTimeout(r, ms)),
    /* The rows fetch is debounced 250ms (DEBOUNCE_MS in static/js/channel-search.js) and
       the facet fetch follows it. Wait out the real debounce - never shorten it, several
       scenarios below are ABOUT debounce generations - then poll for the request list to
       stop moving instead of sleeping a flat 450ms. The stub answers synchronously, so
       both requests land within a microtask of the timer firing and the remaining ~180ms
       was pure slack, 57 settles over. 450ms stays as a hard cap, so a scenario that
       really does take that long (anything with parked requests) behaves exactly as it
       did before. */
    settle: async () => {
      const CAP = 450;
      const t0 = Date.now();
      await new Promise((r) => setTimeout(r, 265));
      let last = -1;
      while (Date.now() - t0 < CAP) {
        if (sent.length === last && held.length === 0) return;
        last = sent.length;
        await new Promise((r) => setTimeout(r, 10));
      }
    },
    /* Poll for a state the page reaches on its own timer, instead of sleeping to just
       past when that timer should have fired. A flat sleep sized to a deadline has only
       its own slack for margin, and on a 2-core box running three shards, real timers
       lose that much routinely - the sleep-based version of the slow-results observation
       below left 50ms and went red under load while passing standalone
       (dev/changelog/724). The cap is what keeps "reached it late" distinguishable from
       "never reached it"; the returned ms (or -1 on timeout) is what says which. */
    waitUntil: async (pred, cap = 4000) => {
      const t0 = Date.now();
      while (Date.now() - t0 < cap) {
        if (pred()) return Date.now() - t0;
        await new Promise((r) => setTimeout(r, 10));
      }
      return -1;
    },
    lastSearch: () => {
      // /counts and /facets excluded the same way /catalog is - each is a different,
      // narrower request (dev/changelog/598, 675), and callers of this helper want the ROW
      // request. Matching on `search?` rather than `search` is what separates them: the two
      // aggregate URLs have a path segment where the row endpoint has its query string.
      const hit = sent.filter((s) => s.method === 'GET'
        && s.url.includes('/api/channels/search?') && !s.url.includes('/catalog')).pop();
      return hit ? new window.URLSearchParams(hit.url.split('?')[1] || '') : null;
    },
    posts: () => sent.filter((s) => s.method === 'POST'),
    bar: () => document.querySelector('#cs-q'),
    // Everything parked by `hold`, answered at once. Returns how many there were, so a
    // scenario asserting "the request really was in flight" has something to check.
    release: () => { const n = held.length; held.splice(0).forEach((h) => h.resolve(h.answer())); return n; },
    rejectHeld: (msg = 'Search failed.') => {
      const n = held.length;
      held.splice(0).forEach((h) => h.reject(new Error(msg)));
      return n;
    },
    held: () => held.length,
    setHold: (g) => { holdGrain = g; },
    setHoldCounts: (v) => { holdCountsFlag = v; },
    // The results area, read the way a person reads it: is the spinner up, are there
    // rows, and does the empty state have the floor.
    loading: () => document.querySelector('#all-loading').style.display !== 'none',
    rowCount: () => document.querySelectorAll('#ch-list .arow, #ch-list .acard').length,
    emptyShown: () => document.querySelector('#all-empty').style.display !== 'none',
  };
  ctx.params = () => {
    const p = ctx.lastSearch();
    return p ? Object.fromEntries([...new Set([...p.keys()])].map((k) => [k, p.getAll(k)])) : {};
  };
  return ctx;
}

const out = {};

/* ── The standing options after the inversion (dev/changelog/778) ──────
   Six of the eight are keyed `show*` and REMOVE rows while their key is absent, so three
   things that used to be one statement are now three different ones: which toggle is lit,
   which option is hiding, and what the disclosure line reports. Every one of them is
   observed here, because a page that got the direction wrong still renders and still
   answers - it just answers the opposite question. */
async function standingInversionScenario() {
  const c = boot();
  await c.settle();
  const obs = {};
  /* One option turned OFF before anything is read, so the lit half and the hiding half
     cannot be the same SIZE. The channel grain's six defaults split 3/3, and a badge
     counting the wrong half would read identically either way - the assertion this
     scenario exists for could not tell them apart (dev/changelog/860). `showmembers` is
     the one to move: it is turned OFF here, so it joins the hiding half rather than
     leaving it, which is what keeps the disclosure entry read below the same one it has
     always been. */
  const seed = c.$('#standcard .st-tog[data-standing="showmembers"]');
  if (seed) { c.click(seed); await c.settle(); }

  obs.disclosure_entries = c.$$('#cs-count .hid').map((el) => el.textContent.trim());
  obs.disclosure_keys = c.$$('#cs-count .hid').map((el) => el.dataset.standingOff);
  // Lit vs. carrying a count. For a `show*` option these are opposites: it hides while it
  // is unlit, and only a hiding option has anything to count.
  obs.all_toggles = c.$$('#standcard .st-tog').map((el) => el.dataset.standing);
  obs.lit_toggles = c.$$('#standcard .st-tog.on').map((el) => el.dataset.standing);
  obs.toggles_with_a_count = c.$$('#standcard .st-tog')
    .filter((el) => el.querySelector('.st-n')).map((el) => el.dataset.standing);
  obs.card_title = (c.$('#standcard h4 .rt') || {}).textContent || '';
  obs.card_badge = (c.$('#standcard h4 .scnt') || {}).textContent || '';

  // Clicking a disclosure entry must PUT THOSE ROWS BACK. The entry only ever renders for
  // an option that is hiding, which for a `show*` key is the state where the key is already
  // absent - so deleting it is a no-op and the click does nothing at all.
  obs.standing_before_disclosure_click = (c.params().standing || []).slice().sort();
  const entry = c.$('#cs-count .hid');
  if (entry) {
    obs.clicked_disclosure_key = entry.dataset.standingOff;
    c.click(entry);
    await c.settle();
    obs.standing_after_disclosure_click = (c.params().standing || []).slice().sort();
  }
  obs.errors = c.errors;
  return obs;
}

/* ── 1. Boot: what a first paint looks like once the JS has run ─────────── */
async function bootScenario() {
  const c = boot();
  await c.settle();
  const heads = c.$$('#ahead > div');
  return {
    errors: c.errors,
    config_is_a_window_property: !!c.window.CHANNEL_SEARCH_CONFIG,
    fetched_catalog: c.sent.some((s) => s.url.includes('/catalog')),
    // Rows are fetched with facets= (count nothing); the rail follows behind on its own
    // endpoint (dev/changelog/676). `search?` matches the ROW endpoint only - `/counts` and
    // `/facets` put a path segment where the row URL puts its query string.
    rows_request: (c.sent.find((s) => s.url.includes('/api/channels/search?')
      && !s.url.includes('/catalog')) || {}).url || '',
    search_request_count: c.sent.filter((s) => s.url.includes('/api/channels/search?')
      && !s.url.includes('/catalog')).length,
    facet_dims_requested: (() => {
      const withFacets = c.sent.filter((s) => s.url.includes('/api/channels/search/facets?'));
      if (!withFacets.length) return [];
      return new URLSearchParams(withFacets.pop().url.split('?')[1]).getAll('facets');
    })(),
    row_count: c.$$('#ch-list .arow').length,
    rail_facets: c.$$('#rail .fac').map((el) => el.dataset.fac),
    rail_has_standing_card: !!c.$('#standcard'),
    rail_uncounted_glyphs: c.$$('#rail .pcount.uncounted').map((el) => el.textContent.trim()),
    rail_zero_counts: c.$$('#rail .pcount.zero').length,
    well_html: c.$('#wellbody').innerHTML,
    words_join_html: (c.$('#wjoin-left') || {}).innerHTML || '',
    column_items: c.$$('#cols-list .col-item').map((el) => el.dataset.col),
    hidden_columns: c.$$('#cols-list input[type=checkbox]')
      .filter((b) => !b.checked).map((b) => b.dataset.col),
    header_labels: heads.map((d) => d.textContent.trim()),
    sortable_headers: c.$$('#ahead [data-sort]').map((h) => h.dataset.sort),
    address_bar: c.window.location.search,
    selection_bar_shown: c.$('#sel-bar').classList.contains('show'),
  };
}

/* ── 2. The search box: chips, scope, suggestions, the + Filter popover ─── */
async function boxScenario() {
  const c = boot();
  await c.settle();
  const obs = {};

  c.type(c.bar(), 'espn wembley');
  await c.settle();
  obs.text_chip_html = c.$('#wellbody').innerHTML;
  obs.text_sent = c.params().q || [];

  c.focus(c.bar());
  const sugg = c.$('#sugg');
  obs.sugg_open = sugg.classList.contains('show');
  obs.sugg_html = sugg.innerHTML;
  obs.sugg_field_switches = c.$$('#sugg input[data-field]').map((el) => el.dataset.field);
  obs.sugg_disabled_switches = c.$$('#sugg input[data-field][disabled]').length;

  /* Ticking scope fields on: the request must carry the registry's order, not the order
     they were clicked in. Description FIRST and then the title, which is the reverse of
     registry order - clicking them the other way round would pass whether or not anything
     re-ordered. Re-queried between clicks because setField re-renders the whole menu. */
  const desc = c.$('#sugg input[data-field="epg-desc"]');
  desc.checked = true;
  c.change(desc);
  await c.settle();
  const title = c.$('#sugg input[data-field="epg-title"]');
  title.checked = true;
  c.change(title);
  await c.settle();
  obs.fields_after_tick = c.params().in || [];

  // ...and turning every one of them off must leave something searched, out loud.
  for (const key of ['epg-desc', 'epg-title', 'name']) {
    const box = c.$(`#sugg input[data-field="${key}"]`);
    if (box) { box.checked = false; c.change(box); await c.wait(20); }
  }
  await c.settle();
  obs.fields_after_all_off = c.params().in || [];
  obs.toasts_after_all_off = c.toasts.slice();

  // The + Filter popover. Every branch of its handler rewrites the panel, which
  // detaches the node that was clicked - so without its own stopPropagation the
  // document closer shuts it on every pick (dev/docs/BUGS.md 2026-07-30 05:15 PM).
  c.click(c.$('#fb-add'));
  await c.wait(30);
  obs.popover_open_after_open = c.$('#fb-pop').classList.contains('open');
  obs.popover_dims = c.$$('#fb-pop-body [data-dim]').map((el) => el.dataset.dim);
  const catDim = c.$('#fb-pop-body [data-dim="cat"]');
  if (catDim) { c.click(catDim); await c.wait(30); }
  obs.popover_open_after_drilling_in = c.$('#fb-pop').classList.contains('open');
  const firstValue = c.$('#fb-pop-body .prow[data-key]');
  obs.popover_offers_values = !!firstValue;
  if (firstValue) {
    obs.picked_value = firstValue.dataset.value;
    c.click(firstValue);
    await c.settle();
  }
  obs.filters_after_pick = c.params()['f.cat'] || [];
  obs.well_after_pick = c.$('#wellbody').innerHTML;

  // Removing the chip takes the filter out again.
  const rm = c.$(`#wellbody [data-rm="cat"]`) || c.$('#wellbody .cx');
  if (rm) { c.click(rm); await c.settle(); }
  obs.filters_after_chip_removed = c.params()['f.cat'] || [];

  // any-word, then Clear all. The control is the joined [data-menu] button's popover
  // since dev/changelog/811 - the segmented copy that used to sit on this line is gone,
  // and the only other live one is inside the Searching-in sheet at phone width.
  c.click(c.$('#wjoin-left [data-menu]'));
  await c.wait(20);
  const any = c.$('#wjoin-left [data-wjoin="any"]');
  if (any) { c.click(any); await c.settle(); }
  obs.match_after_any = c.params().match || [];
  c.click(c.$('#cs-clear'));
  await c.settle();
  obs.after_clear_all = c.params();
  obs.errors = c.errors;
  return obs;
}

/* A "Filter by" suggestion must never offer a value that would return 0 results
   (dev/docs/BUGS.md 2026-08-12): buildSuggestions() scans the full dimension vocabulary
   for a label match with no regard for the facet counts already on hand, so a value
   genuinely absent from the loaded facets - Beta has zero channels, so its account id is
   absent from `facets.acct` entirely, not merely uncounted - was still being offered.
   Boots with a fixture whose `acct` facet is real and counted (`facets_counted` includes
   `acct`) so `countOf()` returns a genuine 0 for Beta and a genuine 4 for Alpha, rather
   than the null "not counted yet" state every other scenario's empty-facets fixture
   produces. */
async function suggestionFacetScenario() {
  const c = boot({ rows: ROWS_ACCTFACETS });
  await c.settle();
  const obs = {};

  c.focus(c.bar());
  c.type(c.bar(), 'beta');
  obs.sugg_html_zero_count = c.$('#sugg').innerHTML;

  c.type(c.bar(), '');
  c.type(c.bar(), 'alpha');
  obs.sugg_html_nonzero_count = c.$('#sugg').innerHTML;

  obs.errors = c.errors;
  return obs;
}

/* ── 3. The table: columns, sorting, selection, row actions ─────────────── */
async function tableScenario() {
  const c = boot({ rows: ROWS_DUP });
  await c.settle();
  const obs = {};
  const heads = () => c.$$('#ahead > div').map((d) => d.textContent.trim());
  const boxFor = (key) => c.$(`#cols-list input[data-col="${key}"]`);

  obs.header_before = heads();
  const cat = boxFor('category');
  cat.checked = true;
  c.change(cat);
  await c.wait(40);
  obs.header_after_showing_category = heads();
  obs.row_cell_count = c.$$('#ch-list .arow')[0].children.length;
  obs.head_cell_count = c.$$('#ahead > div').length;
  obs.visible_column_count = c.$$('#cols-list input[type=checkbox]').filter((b) => b.checked).length;
  const colPost = c.posts().filter((p) => p.url.includes('/api/user-prefs/')).pop();
  obs.column_pref_url = colPost ? colPost.url : '';
  obs.column_pref_value = colPost ? colPost.body.value : null;

  // Sorting, and the guard: hiding the column a sort runs on.
  const catHead = c.$$('#ahead [data-sort]').find((h) => h.dataset.sort === 'category');
  obs.category_is_sortable = !!catHead;
  if (catHead) { c.click(catHead); await c.settle(); }
  obs.sort_after_click = c.params().sort || [];
  c.toasts.length = 0;
  cat.checked = false;
  c.change(cat);
  await c.settle();
  obs.sort_after_hiding_the_column = c.params().sort || [];
  obs.page_after_hiding_the_column = c.params().page || [];
  obs.toasts_after_hiding_the_column = c.toasts.slice();

  // The four columns the engine cannot sort on must render unsorted.
  for (const key of ['groups', 'tags']) { const b = boxFor(key); if (b && !b.checked) { b.checked = true; c.change(b); } }
  await c.wait(40);
  obs.sortable_headers = c.$$('#ahead [data-sort]').map((h) => h.dataset.sort);
  obs.catalog_sorts = CATALOG.sorts;

  // Selection: a Map that survives a search, and says so.
  const rowBoxes = () => c.$$('#ch-list input[type=checkbox]');
  const first = rowBoxes()[0];
  first.checked = true;
  c.change(first);
  await c.wait(20);
  obs.bar_shown_at_one = c.$('#sel-bar').classList.contains('show');
  obs.buttons_at_one = ['#sel-group', '#sel-add', '#sel-hide'].map((s) => c.$(s).textContent.trim());
  c.click(c.$('#sel-all-btn'));
  await c.wait(20);
  obs.page_row_count = rowBoxes().length;
  obs.all_rows_ticked = rowBoxes().every((b) => b.checked);
  obs.header_tick_reflects_page = c.$('#all-select-all').checked;
  obs.buttons_after_select_all = c.$('#sel-group').textContent.trim();
  c.type(c.bar(), 'zzz-matches-nothing');
  await c.settle();
  obs.buttons_after_a_new_search = c.$('#sel-group').textContent.trim();
  obs.selection_note = c.$('#sel-note').textContent.trim();
  c.click(c.$('#desel-all-btn'));
  await c.wait(20);
  obs.bar_after_deselect = c.$('#sel-bar').classList.contains('show');
  obs.note_after_deselect = c.$('#sel-note').textContent.trim();
  c.type(c.bar(), '');
  await c.settle();

  // The DUP drill-in. It goes by channel id: the row payload MASKS stream URLs, so
  // searching the URL on screen would match nothing.
  const before = c.window.location.search;
  const badge = c.$('#ch-list [data-act="dup-badge"]');
  obs.dup_badge_rendered = !!badge;
  if (badge) {
    const row = ROWS_DUP.rows.find((r) => r.id === Number(badge.dataset.id));
    obs.dup_payload_ids = (row.dup || {}).ids || [];
    obs.dup_payload_count = (row.dup || {}).count;
    c.click(badge);
    await c.settle();
    obs.drill_in_filters = c.params()['f.chan'] || [];
    obs.drill_in_standing = c.params().standing || [];
    obs.back_button_offered = !!c.$('#cs-back');
    if (c.$('#cs-back')) {
      c.click(c.$('#cs-back'));
      await c.settle();
      obs.address_bar_after_back = c.window.location.search;
      obs.address_bar_before_drill_in = before;
      obs.back_button_after_use = !!c.$('#cs-back');
    }
  }

  /* The third guide state (dev/changelog/759). `discovery` is a member of the in-guide
     group "Fox" with no guide row of its own, so it is the row whose every control used
     to say "+ Add to Guide" while the "In your guide" filter matched it. */
  const rowFor = (name) => c.$$('#ch-list .arow')
    .find((r) => (r.querySelector('.a-name') || {}).textContent === name);
  const viaRow = rowFor('Discovery Channel');
  obs.guide_via_row_found = !!viaRow;
  if (viaRow) {
    obs.guide_via_badges = Array.from(viaRow.querySelectorAll('.badge'))
      .map((b) => b.textContent.trim()).filter((t) => t.startsWith('In guide via'));
    // WHERE it renders is the assertion, not just that it renders: every cell except the
    // name cell is `overflow: hidden; white-space: nowrap` over a track sized without
    // regard to a group's name, so a variable-width badge carrying one would be clipped
    // mid-word there and jsdom would never see it.
    obs.guide_via_in_name_cell = !!viaRow.querySelector('.a-namecell .badge');
    obs.guide_via_outside_name_cell = Array.from(
      viaRow.querySelectorAll('.acell:not(.a-namecell) .badge'))
      .some((b) => b.textContent.trim().startsWith('In guide via'));
    // It is a LINK to the group it names, not to the channel the row is about
    // (dev/changelog/860): a badge whose whole content is a group's name is an address.
    const badge = Array.from(viaRow.querySelectorAll('.a-namecell a'))
      .find((a) => a.textContent.trim().startsWith('In guide via'));
    obs.guide_via_href = badge ? badge.getAttribute('href') : '';
    obs.guide_via_tip = badge ? (badge.getAttribute('data-tip') || '') : '';
  }
  const plainRow = rowFor('Test Card');
  obs.plain_row_badges = plainRow
    ? Array.from(plainRow.querySelectorAll('.badge')).map((b) => b.textContent.trim()) : null;

  /* The Account cell's name is a link to the account, for the same reason. It is looked up
     on the row rather than in the header, because the cell is what has to carry it. */
  const firstRow = c.$$('#ch-list .arow:not(.is-group)')[0];
  const acctLink = firstRow
    ? firstRow.querySelector('.acell:not(.a-namecell) a[href^="/accounts/"]') : null;
  obs.account_cell_href = acctLink ? acctLink.getAttribute('href') : '';

  obs.open_channel_targets = c.$$('#ch-list [data-act="open-channel"]').length;
  obs.errors = c.errors;
  return obs;
}

/* ── 3b. The pinned name column's frozen width ───────────────────────────
   The header and every row are separate grid containers, so this column is measured
   once and written into #col-css as ONE px value they all share. What it measures is
   the CELL's content extent - not the track the cell currently sits in, which reports
   the column's own min-width and so can never notice that the content outgrew it
   (BUGS.md 2026-08-20 09:12). */
async function nameColScenario() {
  const obs = {};
  const thirdTrack = (c) => {
    const css = (c.$('#col-css') || {}).textContent || '';
    const m = /grid-template-columns:\s*\S+\s+\S+\s+(\S+)/.exec(css);
    return m ? m[1] : '';
  };
  // Nothing to measure: production behavior under a layout-less DOM, and the state
  // every other scenario in this file runs in.
  const unmeasured = boot({ rows: ROWS_DUP });
  await unmeasured.settle();
  obs.track_unmeasured = thirdTrack(unmeasured);
  // A cell wider than the column's min-width: the column has to follow the content.
  const measured = boot({ rows: ROWS_DUP, pinnedWidth: 310 });
  await measured.settle();
  obs.track_measured = thirdTrack(measured);
  // Past the cap, which exists so this column cannot push the table into a horizontal
  // scroll (dev/changelog/414). The cap wins; the cell is clipped instead.
  const huge = boot({ rows: ROWS_DUP, pinnedWidth: 900 });
  await huge.settle();
  obs.track_clamped = thirdTrack(huge);
  obs.errors = unmeasured.errors.concat(measured.errors, huge.errors);
  return obs;
}

/* ── 4. Saved searches, and the action context ──────────────────────────── */
async function savedScenario() {
  const saved = [
    { name: 'Needs a check',
      params: 'in=name&in=epg-title&f.health=poor&f.health=untested&sort=category',
      is_default: false },
    // The case the "a saved search carries the standing options" rule exists for: you
    // cannot clean up duplicates while a standing option is hiding them. `showdup` is
    // therefore PRESENT - since the inversion (dev/changelog/778) the key means show, and
    // a bare `standing=` would hide the very rows this search exists to clean up.
    // per_page=1 is in it so the pager has a Next to click: the pager renders only when
    // there are more matches than one page holds, and this corpus is seven rows.
    { name: 'Duplicate cleanup',
      params: 'in=name&f.other=dupurl&standing=showdup&standing=showhidden'
              + '&standing=shownoepg&standing=showuntested&sort=category&per_page=1',
      is_default: true },
  ];
  const c = boot({ rows: ROWS_PAGED, saved });
  await c.settle();
  const obs = {};

  c.click(c.$('#sf-btn'));
  await c.wait(30);
  obs.listed_names = c.$$('#sf-pop-body .prow .pv').map((el) => el.textContent.replace(/\s+/g, ' ').trim());
  obs.default_badges = c.$$('#sf-pop-body .prow .badge').length;
  obs.default_button_labels = c.$$('#sf-pop-body [data-default]').map((b) => b.textContent.trim());

  c.click(c.$('#sf-pop-body [data-load="1"]'));
  await c.settle();
  obs.loaded_filters = c.params()['f.other'] || [];
  obs.loaded_standing = c.params().standing || [];
  obs.panel_open_after_load = c.$('#sf-pop').classList.contains('open');
  obs.current_name_after_load = c.$('#sf-current').textContent.trim();
  obs.dirty_after_load = c.$('#sf-dirty').style.display;

  c.type(c.bar(), 'espn');
  await c.settle();
  obs.dirty_after_editing = c.$('#sf-dirty').style.display;
  obs.name_after_editing = c.$('#sf-current').textContent.trim();
  c.type(c.bar(), '');
  await c.settle();
  obs.dirty_after_undoing_the_edit = c.$('#sf-dirty').style.display;

  const next = c.$$('#pager [data-pg]').find((b) => b.dataset.pg === 'next');
  obs.pager_has_next = !!(next && !next.disabled);
  if (next && !next.disabled) {
    c.click(next);
    await c.settle();
    obs.dirty_after_paging = c.$('#sf-dirty').style.display;
    obs.page_after_paging = c.params().page || [];
  }

  c.click(c.$('#sf-btn'));
  await c.wait(30);
  obs.name_box_prefill = c.$('#sf-newname').value;
  c.type(c.$('#sf-newname'), 'My new search');
  let n = c.posts().length;
  c.click(c.$('#sf-pop-body [data-save]'));
  await c.wait(60);
  const savePost = c.posts().slice(n)[0] || {};
  obs.save_post_url = savePost.url || '';
  obs.saved_names = (savePost.body ? savePost.body.value : []).map((s) => s.name);
  const savedParams = new c.window.URLSearchParams(
    ((savePost.body ? savePost.body.value : []).slice(-1)[0] || {}).params || '');
  obs.saved_record_standing = savedParams.getAll('standing');
  obs.saved_record_filters = savedParams.getAll('f.other');
  obs.saved_record_has_facets = savedParams.has('facets');
  obs.saved_record_has_page = savedParams.has('page');
  obs.panel_open_after_save = c.$('#sf-pop').classList.contains('open');
  obs.current_name_after_save = c.$('#sf-current').textContent.trim();

  n = c.posts().length;
  c.click(c.$('#sf-pop-body [data-default="2"]'));
  await c.wait(30);
  obs.defaults_after_set = ((c.posts().slice(n)[0] || {}).body.value || []).map((s) => !!s.is_default);
  n = c.posts().length;
  c.click(c.$('#sf-pop-body [data-default="2"]'));
  await c.wait(30);
  obs.defaults_after_clear = ((c.posts().slice(n)[0] || {}).body.value || []).map((s) => !!s.is_default);

  n = c.posts().length;
  c.click(c.$('#sf-pop-body [data-del="0"]'));
  await c.wait(30);
  obs.names_after_delete = ((c.posts().slice(n)[0] || {}).body.value || []).map((s) => s.name);
  obs.panel_open_after_delete = c.$('#sf-pop').classList.contains('open');

  c.click(c.$('#cs-clear'));
  await c.settle();
  obs.current_name_after_clear_all = c.$('#sf-current').textContent.trim();
  obs.dirty_after_clear_all = c.$('#sf-dirty').style.display;
  obs.errors = c.errors;
  return obs;
}

/* ── Rows per page (dev/changelog/1043) ─────────────────────────────────
   The Settings default reaches the page through the catalog's `opening_page_size`, never
   through the engine's own meaning of a missing `per_page`; a URL that names a size wins;
   the dropdown changes this search only and starts it again from page 1. */
async function pageSizeScenario() {
  const obs = {};
  const at250 = Object.assign({}, CATALOG, { opening_page_size: 250 });
  const sizeOf = (c) => (c.params().per_page || [])[0];
  const menu = (c) => c.$('#pager #pg-size');

  let c = boot();
  await c.settle();
  obs.stock_request_size = sizeOf(c);
  obs.errors = c.errors.slice();

  c = boot({ catalog: at250 });
  await c.settle();
  obs.configured_request_size = sizeOf(c);
  obs.errors.push(...c.errors);

  c = boot({ catalog: at250, url: 'http://localhost:5000/channels?per_page=1', rows: ROWS_PAGED });
  await c.settle();
  obs.url_request_size = sizeOf(c);
  obs.url_menu_value = menu(c) ? menu(c).value : null;
  obs.url_menu_options = menu(c) ? Array.from(menu(c).options).map((o) => o.value) : [];
  const next = c.$$('#pager [data-pg]').find((b) => b.dataset.pg === 'next');
  if (next) { c.click(next); await c.settle(); }
  obs.page_before_change = (c.params().page || [])[0];
  const n = c.posts().length;
  if (menu(c)) {
    menu(c).value = '250';
    c.change(menu(c));
    await c.settle();
  }
  obs.changed_request_size = sizeOf(c);
  obs.page_after_change = (c.params().page || [])[0];
  obs.posts_on_change = c.posts().length - n;
  obs.errors.push(...c.errors);

  // 180 matches at 250 a page: no page turns, but 100 would page, so the menu stays.
  const fits = Object.assign({}, ROWS, { total: 180, pages: 1 });
  c = boot({ rows: fits, url: 'http://localhost:5000/channels?per_page=250' });
  await c.settle();
  obs.fits_has_turns = c.$$('#pager [data-pg]').length > 0;
  obs.fits_has_menu = !!menu(c);
  obs.errors.push(...c.errors);

  // Everything fits in the smallest size: nothing to page and nothing to choose.
  c = boot();
  await c.settle();
  obs.small_pager_html = c.$('#pager').innerHTML.trim();
  obs.errors.push(...c.errors);
  return obs;
}

/* ── 5. The action context: an errand, never a filter ───────────────────── */
async function contextScenario() {
  const group = (CATALOG.groups || [])[0] || { id: 1, name: 'Group' };
  const c = boot({ url: `http://localhost:5000/channels?add_to_group=${group.id}` });
  await c.settle();
  const obs = {
    group_name_from_catalog: group.name,
    group_id_from_catalog: String(group.id),
    context_shown: c.$('#sel-ctx').style.display !== 'none',
    context_label: c.$('#sel-ctx-lbl').textContent.trim(),
    bar_shown_at_zero_selected: c.$('#sel-bar').classList.contains('show'),
    // The context is not a filter: it never becomes a chip and never narrows the search.
    well_html: c.$('#wellbody').innerHTML,
    add_to_group_in_search_request: (c.params().add_to_group || []),
    search_params: Object.keys(c.params()),
  };
  c.click(c.$('#sel-ctx-x'));
  await c.settle();
  obs.context_after_dismiss = c.$('#sel-ctx').style.display !== 'none';
  obs.address_bar_after_dismiss = c.window.location.search;
  obs.errors = c.errors;
  return obs;
}

/* ── 5b. The one bulk guide action, and the three answers a selection can give ──
   `in_guide` is per-channel, so a selection can hold both answers at once. The action is
   one button that becomes Add or Remove, and DISABLES on a mixed selection while saying
   why (dev/changelog/792). Nothing about that is reachable from Python: the label, the
   disabled flag and the note are all written by updateSelectionUI over a Map that lives
   only in the page. */
async function guideActionScenario() {
  const c = boot();
  await c.settle();
  const obs = {};
  /* CHANNEL rows only. A group row carries `in_guide` too and its id is a GROUP id, so
     including them puts group ids into a set that is then asked about channel ids - and
     the two id spaces overlap, so the scenario would pick a channel that is not in the
     guide and assert against the wrong half of the fixture. */
  const inGuide = new Set(
    ROWS.rows.filter((r) => r.kind !== 'group' && r.in_guide).map((r) => r.id));
  const boxes = () => c.$$('#ch-list input[type=checkbox]')
    .filter((b) => b.dataset.id !== undefined);
  const boxFor = (pred) => boxes().find((b) => pred(inGuide.has(Number(b.dataset.id))));
  const tick = async (box, on) => { box.checked = on; c.change(box); await c.wait(20); };
  const read = () => ({
    label: c.$('#sel-add').textContent.trim(),
    disabled: !!c.$('#sel-add').disabled,
    note: c.$('#sel-guide-note').textContent.trim(),
  });

  const outBox = boxFor((v) => !v);
  const inBox = boxFor((v) => v);
  obs.fixture_has_both = !!outBox && !!inBox;
  if (!obs.fixture_has_both) { obs.errors = c.errors; return obs; }

  obs.nothing_selected = read();
  await tick(outBox, true);
  obs.only_not_in_guide = read();
  await tick(inBox, true);
  obs.mixed = read();
  await tick(outBox, false);
  obs.only_in_guide = read();

  // The action itself, from the all-in-guide state: ONE request for the whole selection.
  const n = c.posts().length;
  c.click(c.$('#sel-add'));
  await c.wait(60);
  const post = c.posts().slice(n)[0] || {};
  obs.remove_post_url = post.url || '';
  obs.remove_post_body = post.body || null;
  obs.remove_post_count = c.posts().slice(n).filter((p) => p.url.includes('/remove')).length;
  obs.toasts_after_remove = c.toasts.slice();
  // Kept, not cleared - undoing an accidental bulk remove has to be one click.
  obs.selection_survives_remove = c.$('#sel-bar').classList.contains('show');

  /* NO ROW CARRIES A GUIDE CONTROL at this width any more (dev/changelog/860) - not a
     channel row, and not a group row either. The checkbox plus this bar is the one path,
     which is what the two counts below stand for. */
  await c.settle();
  obs.row_guide_controls = c.$$('#ch-list [data-act="add-guide"], #ch-list [data-act="in-guide"]').length;
  obs.group_guide_controls = c.$$(
    '#ch-list [data-act="group-add-guide"], #ch-list [data-act="group-in-guide"]').length;
  obs.group_rows_on_screen = c.$$('#ch-list .arow.is-group').length;
  obs.errors = c.errors;
  return obs;
}

/* ── 6. Phone width: cards, the chip row, and no rail ────────────────────
   The same page, the same state, the same requests - only the drawing changes.
   `width: 375` is what the matchMedia stub above answers from. */
async function mobileScenario() {
  const c = boot({ width: 375 });
  await c.settle();
  const obs = {
    // The rail is NOT BUILT down here, rather than built and hidden: on a phone the
    // rail and + Filter are the same thing, so a populated one would be a second,
    // invisible copy of every filter control.
    rail_html: c.$('#rail').innerHTML.trim(),
    table_rows: c.$$('#ch-list .arow').length,
    card_count: c.$$('#ch-list .ccard').length,
    chips: c.$$('#chiprow [data-mchip]').map((el) => el.dataset.mchip),
    sort_chip_text: (c.$('[data-mchip="sort"]') || {}).textContent || '',
    scope_chip_text: (c.$('[data-mchip="scope"]') || {}).textContent || '',
    cols_btn_label: c.$('#cols-btn').textContent.trim(),
    // The name is its own full-width line and is never truncated (DESIGN.md 9.4).
    first_card_name: (c.$('#ch-list .ccard .cc-name') || {}).textContent || '',
    // Two fixed tracks: a short label, then the value. The labels are what round 3
    // was picked to add - an unlabeled card is the thing that could not be deciphered.
    first_card_labels: c.$$('#ch-list .ccard').length
      ? Array.from(c.$$('#ch-list .ccard')[0].querySelectorAll('.cr-l')).map((el) => el.textContent.trim())
      : [],
    first_card_label_value_pairs: c.$$('#ch-list .ccard').length
      ? [c.$$('#ch-list .ccard')[0].querySelectorAll('.cr-l').length,
         c.$$('#ch-list .ccard')[0].querySelectorAll('.cr-v').length]
      : [],
    // Every card, both kinds: a channel card's kebab carries its channel actions and a
    // GROUP card's carries the group's (dev/changelog/811). What the assertion is about is
    // that no card is left with nowhere to put its single-row actions.
    every_card_has_a_kebab: c.$$('#ch-list .ccard').length
      === c.$$('#ch-list .ccard [data-rowkebab], #ch-list .ccard [data-groupkebab]').length,
    no_per_card_add_button: c.$$('#ch-list .ccard [data-act="add-guide"]').length,
  };

  // Selecting: short labels, and the body class that gives the fixed bar its room.
  const cb = c.$('#ch-list .ccard input[type=checkbox]');
  cb.checked = true;
  c.change(cb);
  obs.sel_bar_labels = ['#sel-add', '#sel-group', '#sel-hide'].map((s) => c.$(s).textContent.trim());
  obs.body_has_selbar_class = c.document.body.classList.contains('has-selbar');

  // The Fields sheet: visibility only, no re-query, and its own pref row.
  c.click(c.$('#cols-btn'));
  await c.wait(20);
  obs.fields_sheet_title = (c.$('.cs-sheet .modal-head h2') || {}).textContent || '';
  obs.fields_sheet_keys = c.$$('.cs-sheet [data-cardfield]').map((el) => el.dataset.cardfield);
  const before = c.sent.length;
  const health = c.$('.cs-sheet [data-cardfield="health"]');
  health.checked = false;
  c.change(health);
  await c.wait(20);
  obs.labels_after_hiding_health = c.$$('#ch-list .ccard')[0]
    ? Array.from(c.$$('#ch-list .ccard')[0].querySelectorAll('.cr-l')).map((el) => el.textContent.trim())
    : [];
  obs.requests_after_hiding_a_field = c.sent.slice(before)
    .map((s) => `${s.method} ${s.url.split('?')[0]}`);
  obs.errors = c.errors;
  return obs;
}

/* ── 7. Phone width: every sheet, driven ─────────────────────────────────- */
async function mobileSheetsScenario() {
  const c = boot({ rows: ROWS_DUP, width: 375 });
  await c.settle();
  const obs = {};
  const sheetBody = () => c.$('.cs-sheet .modal-body');
  const sheetTitle = () => ((c.$('.cs-sheet .modal-head h2') || {}).textContent || '').trim();

  /* The Filters sheet: the rail's two-level replacement, plus Fixed Exclusions as
     its footer rather than a card of its own. */
  c.click(c.$('#addf2'));
  await c.wait(20);
  obs.filters_title = sheetTitle();
  obs.filters_dims = c.$$('.cs-sheet [data-fdim]').map((el) => el.dataset.fdim);
  obs.filters_standing_at_top_level = c.$$('.cs-sheet [data-standing]').map((el) => el.dataset.standing);
  c.click(c.$('.cs-sheet [data-fdim="acct"]'));
  await c.wait(20);
  obs.after_drill_title = sheetTitle();
  obs.after_drill_has_back = !!c.$('.cs-sheet [data-fback]');
  obs.after_drill_value_rows = c.$$('.cs-sheet .prow[data-key]').length;
  obs.after_drill_tri_pairs = c.$$('.cs-sheet .tri button[data-dir]').length;
  // Excluding from the sheet's three-state, which is what the one-tap suggestion
  // row down here deliberately cannot do.
  const exc = c.$$('.cs-sheet .tri button[data-dir="exc"]')[0];
  if (exc) c.click(exc);
  await c.settle();
  obs.request_after_exclude = c.params();
  obs.sheet_still_open_after_picking = !!c.$('.cs-sheet');
  obs.chip_count_after_exclude = (c.$('#well-lbl-slot .nsel') || {}).textContent || '';
  c.click(c.$('.cs-sheet [data-fback]'));
  await c.wait(20);
  obs.after_back_dims = c.$$('.cs-sheet [data-fdim]').map((el) => el.dataset.fdim);
  // A standing option, from the sheet's footer.
  c.click(c.$('.cs-sheet [data-standing="showdup"]'));
  await c.settle();
  obs.standing_after_toggle = (c.params().standing || []);
  // The Filters sheet's entry point is the well's `+ Filter` since dev/changelog/811, and
  // that button is in the page BODY, under the open sheet - so it closes by its own X,
  // which is what a real tap can actually reach.
  c.click(c.$('.cs-sheet .modal-head button'));
  await c.wait(20);
  obs.filters_sheet_closed_by_its_x = !c.$('.cs-sheet');

  /* Searching in: the desktop popover's left pane, as its own sheet. */
  c.click(c.$('[data-mchip="scope"]'));
  await c.wait(20);
  // A CHIP still toggles its own sheet - the chip row stays above the sheet, so the tap
  // reaches it. This is the half of that rule that survives.
  c.click(c.$('[data-mchip="scope"]'));
  await c.wait(20);
  obs.sheet_closed_by_its_own_chip = !c.$('.cs-sheet');
  c.click(c.$('[data-mchip="scope"]'));
  await c.wait(20);
  obs.scope_title = sheetTitle();
  obs.scope_fields = c.$$('.cs-sheet [data-field]').map((el) => el.dataset.field);
  obs.scope_has_match_segment = c.$$('.cs-sheet [data-wjoin]').map((el) => el.dataset.wjoin);
  const desc = c.$('.cs-sheet [data-field="epg-desc"]');
  desc.checked = true;
  c.change(desc);
  await c.settle();
  obs.fields_after_tick = c.params().in || [];
  obs.box_focused_after_tick = c.document.activeElement === c.bar();
  c.click(c.$('.cs-sheet [data-wjoin="any"]'));
  await c.settle();
  obs.match_after_any = c.params().match || [];

  /* Sort: the entry point the column headers used to be. */
  c.click(c.$('[data-mchip="sort"]'));
  await c.wait(20);
  obs.sort_options = c.$$('.cs-sheet [data-sortkey]').map((el) => el.dataset.sortkey);
  // `category`, deliberately NOT the boot default: picking the field that is already
  // active is a re-pick, so with `name` here (the default since dev/changelog/699) the
  // first click measured the reversal rule instead of the pick rule.
  c.click(c.$('.cs-sheet [data-sortkey="category"]'));
  await c.settle();
  obs.sort_after_pick = c.params().sort || [];
  c.click(c.$('.cs-sheet [data-sortkey="category"]'));
  await c.settle();
  obs.sort_after_repick = c.params().sort || [];
  // ...and a DIFFERENT field must start ascending rather than inherit that descending.
  c.click(c.$('.cs-sheet [data-sortkey="health"]'));
  await c.settle();
  obs.sort_after_switching_field = c.params().sort || [];
  c.click(c.$('.cs-sheet .modal-close'));
  await c.wait(20);

  /* The suggestion menu at 375: ONE pane, plus the scope footer that is the only
     thing left linking what you type to what it matches. */
  c.type(c.bar(), 'es');
  c.focus(c.bar());
  await c.settle();
  obs.sugg_open = c.$('#sugg').classList.contains('show');
  obs.sugg_has_scope_pane = !!c.$('#suggscope');
  obs.sugg_has_scope_footer = !!c.$('#sugg [data-openscope]');
  obs.sugg_tri_buttons = c.$$('#sugg .tri button').length;
  // OPENING ANY OVERLAY CLOSES THE MENU. It is not a .menu, so nothing else does,
  // and a sheet opening over it hands back a page covered in stale suggestions
  // when the sheet is dismissed (dev/changelog/394).
  c.click(c.$('#addf2'));
  await c.wait(20);
  obs.sugg_open_after_a_sheet_opened = c.$('#sugg').classList.contains('show');
  c.click(c.$('.cs-sheet .modal-close'));
  await c.wait(20);

  /* The row kebab: every single-channel action, and the selection written through
     the same Map the checkbox writes. */
  const kebab = c.$('#ch-list .ccard [data-rowkebab]');
  const rowId = Number(kebab.dataset.rowkebab);
  c.click(kebab);
  await c.wait(20);
  obs.row_sheet_actions = c.$$('.cs-sheet .prow[data-act]').map((el) => el.dataset.act);
  obs.row_sheet_has_select = !!c.$('.cs-sheet [data-rowsel]');
  obs.row_sheet_has_test = !!c.$('.cs-sheet [data-rowtest]');
  c.click(c.$('.cs-sheet [data-rowsel]'));
  await c.wait(20);
  obs.card_ticked_after_selecting_in_the_sheet =
    c.$(`#ch-list .ccard input[data-id="${rowId}"]`).checked;
  obs.row_sheet_says_deselect_now = sheetBody().textContent.includes('Deselect this channel');
  c.click(c.$('.cs-sheet .modal-close'));
  await c.wait(20);

  /* The sheet's guide row is the ONE per-row guide control left on this page - the
     desktop row's went in dev/changelog/860 - so this is where the single-channel remove
     endpoint is covered. It used to synthesize a form POST to /channels/<id>/toggle and
     reload the whole page, which no fetch stub would ever see. */
  const inGuideCard = ROWS.rows.find((r) => r.kind !== 'group' && r.in_guide);
  obs.sheet_remove_offered = false;
  if (inGuideCard) {
    c.click(c.$(`#ch-list .ccard [data-rowkebab="${inGuideCard.id}"]`));
    await c.wait(20);
    const removeRow = c.$('.cs-sheet [data-act="in-guide"]');
    obs.sheet_remove_offered = !!removeRow;
    if (removeRow) {
      const m = c.posts().length;
      c.click(removeRow);
      await c.wait(60);
      const rp = c.posts().slice(m)[0] || {};
      obs.sheet_remove_post_url = rp.url || '';
      obs.sheet_remove_post_body = rp.body || null;
    }
    await c.settle();
  }

  /* The DUP badge opens a SHEET here rather than drilling straight in: touch has
     no hover, and the hover tooltip is what said what the cluster was. */
  const dupRow = ROWS_DUP.rows.find((r) => r.dup);
  if (dupRow) {
    // The CARD's own badge, not the row sheet's entry - that is the one the desktop
    // handler drills straight in from, so it is the branch the width has to change.
    c.click(c.$(`#ch-list .ccard[data-id="${dupRow.id}"] [data-act="dup-badge"]`));
    await c.settle();
    obs.card_badge_opened_a_sheet = !!c.$('.cs-sheet');
    obs.card_badge_did_not_drill_in = !(c.params()['f.chan'] || []).length;
    if (c.$('.cs-sheet')) c.click(c.$('.cs-sheet .modal-close'));
    await c.wait(20);
    c.click(c.$(`#ch-list .ccard [data-rowkebab="${dupRow.id}"]`));
    await c.wait(20);
    c.click(c.$('.cs-sheet [data-act="dup-badge"]'));
    await c.wait(20);
    obs.dup_sheet_title = sheetTitle();
    obs.dup_sheet_lists_others = c.$$('.cs-sheet .prow .pv').map((el) => el.textContent.trim());
    obs.dup_sheet_says_kept_reason = sheetBody().textContent.includes(dupRow.dup.kept_reason);
    obs.dup_sheet_has_drill_button = !!c.$('.cs-sheet [data-dupdrill]');
    c.click(c.$('.cs-sheet [data-dupdrill]'));
    await c.settle();
    obs.after_drill_params = c.params();
    obs.dup_sheet_closed_after_drill = !c.$('.cs-sheet');
    obs.dup_cluster_ids = (dupRow.dup.ids || []).map(String);
  }
  obs.errors = c.errors;
  return obs;
}

/* ── 8. The AIRING grain: the toggle, and what a showing draws ─────────── */
async function airingScenario() {
  const obs = {};
  const c = boot();
  await c.settle();

  const tabs = () => c.$$('#grainslot-panel .grainpill').map((b) => b.textContent.trim());
  const activeTab = () => (c.$('#grainslot-panel .grainpill.on') || {}).textContent;
  obs.tabs = tabs();
  obs.tab_active_at_boot = (activeTab() || '').trim();
  obs.grain_absent_from_default_request = !(c.params().grain || []).length;

  // Type something and pick a filter FIRST, so the flip has state to carry.
  c.type(c.bar(), 'wembley');
  await c.settle();
  obs.q_before_flip = (c.params().q || [])[0];

  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  await c.settle();

  obs.grain_after_flip = (c.params().grain || [])[0];
  obs.tab_active_after_flip = (activeTab() || '').trim();
  // The typed text, the scope and the match mode all carry - only the RESULT SHAPE changes.
  obs.q_after_flip = (c.params().q || [])[0];
  obs.fields_after_flip = c.params().in || [];
  // A sort the user never PICKED does not cross, even when the target grain can express
  // it. `category` is a valid airing sort AND the channel grain's boot default, so before
  // dev/changelog/692 it followed the flip and the airing grain's own default (`when`) was
  // unreachable by clicking the tab. An EXPLICIT sort still crosses - driven below.
  obs.sort_after_flip = (c.params().sort || [])[0];
  obs.standing_after_flip = (c.params().standing || []).slice().sort();
  // The channel default is `name`, which no airing can be ordered by, so this plain tab
  // click takes the "cannot cross" branch every single time. It must still remap SILENTLY:
  // a note explaining a fallback from a sort nobody picked would fire on every flip
  // (dev/changelog/699).
  obs.count_line_after_flip = (c.$('#cs-count') || {}).textContent || '';

  // The header is the airing registry's, and the pinned column is the PROGRAM.
  obs.head_labels = c.$$('#ahead > div').map((d) => d.textContent.replace(/[▲▼]/g, '').trim());
  obs.sortable_heads = c.$$('#ahead [data-sort]').map((el) => el.dataset.sort);

  const rows = c.$$('#ch-list .arow-air');
  obs.row_count = rows.length;
  obs.rows_are_airing_rows = rows.length === c.$$('#ch-list .arow').length;
  obs.first_row_has_when = !!c.$('#ch-list .arow-air .a-when');
  obs.first_row_title = (c.$('#ch-list .arow-air .a-ptitle') || {}).textContent;
  // The checkbox carries BOTH ids: the selection is a set of channels on both grains.
  const cb = c.$('#ch-list .arow-air input[type=checkbox]');
  obs.checkbox_has_channel_id = !!(cb && cb.dataset.id);
  obs.checkbox_has_airing_id = !!(cb && cb.dataset.air);

  // Every record state the seeded corpus produces, and what each drew.
  obs.record_states = (ROWS_AIRINGS.rows || []).map((r) => r.record_state);
  obs.record_buttons = c.$$('#ch-list .arow-air .a-act button').map((b) => ({
    label: b.textContent.trim(), act: b.dataset.act || '', disabled: b.disabled,
  }));

  // Selecting a showing selects its CHANNEL, and two showings on one channel are one
  // selection - which is what makes the count honest.
  c.$$('#ch-list .arow-air input[type=checkbox]').forEach((box) => {
    box.checked = true;
    box.dispatchEvent(new c.window.Event('change', { bubbles: true }));
  });
  obs.selected_count_text = (c.$('#sel-group') || {}).textContent;
  obs.distinct_channels_in_rows =
    new Set((ROWS_AIRINGS.rows || []).map((r) => (r.channel || {}).id)).size;

  // The Record click fetches its context rather than trusting the masked row payload.
  const openedWith = [];
  c.window.openModal = (prog, ch) => openedWith.push({ prog, ch });
  const recBtn = c.$('#ch-list .arow-air [data-act="rec-new"]');
  if (recBtn) {
    c.click(recBtn);
    await c.settle();
    obs.record_click_fetched_context =
      c.sent.some((x) => x.url.includes('/record-context'));
    obs.record_click_opened_modal = openedWith.length;
    obs.record_modal_got_raw_url =
      !!(openedWith[0] && String(openedWith[0].prog.stream_url).indexOf('***') < 0);
  }

  // Back to channels: the sort remap. Sort by When first, which channels cannot express.
  const whenHead = c.$('#ahead [data-sort="when"]');
  if (whenHead) { c.click(whenHead); await c.settle(); }
  obs.sort_before_back = (c.params().sort || [])[0];
  c.click(c.$('#grainslot-panel [data-grain="channels"]'));
  await c.settle();
  obs.sort_after_back = (c.params().sort || [])[0];
  obs.count_line_after_back = (c.$('#cs-count') || {}).textContent || '';
  obs.head_labels_after_back = c.$$('#ahead > div').map((d) => d.textContent.replace(/[▲▼]/g, '').trim());

  // The other half of the rule above: a sort the user PICKED still crosses. `health` is in
  // both registries, so nothing forces a remap - only the explicit/default distinction
  // decides, and losing a chosen sort on every flip would be the real regression.
  const healthHead = c.$('#ahead [data-sort="health"]');
  if (healthHead) { c.click(healthHead); await c.settle(); }
  obs.sort_chosen_on_channels = (c.params().sort || [])[0];
  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  await c.settle();
  obs.sort_after_flip_when_chosen = (c.params().sort || [])[0];

  obs.errors = c.errors;
  return obs;
}

/* ── 8b. Ctrl-click and middle-click open a new tab, on both grains ─────────
   The airing grain's program title was a <span> and its row navigated by assigning
   location.href, so Ctrl-click replaced the page (dev/docs/BUGS.md 2026-09-16 @ 10:27:44 AM,
   dev/changelog/996). Only MODIFIED clicks are driven here: jsdom implements no navigation
   and reports a plain one as an error, which this harness counts as a page failure. */
async function navClickScenario() {
  const obs = {};
  const probe = async (url, rowSel) => {
    const c = boot({ url });
    await c.settle();
    const opened = [];
    c.window.open = (u, target) => { opened.push({ url: u, target }); return {}; };
    const CFG = c.window.CHANNEL_SEARCH_CONFIG;
    const row = c.$$(`#ch-list ${rowSel}`).find((r) => !r.dataset.group);
    const out = { row_found: !!row, opened_by: {} };
    if (!row) { out.errors = c.errors; return out; }
    out.expected_url = `${CFG.channelUrlBase}${row.dataset.id}`;
    // A cell with no link, button or input in it: the row itself is what is clicked.
    const cell = Array.from(row.querySelectorAll('.acell'))
      .find((el) => !el.querySelector('a, button, input, label') && !el.closest('a'));
    const fire = (el, type, init) => {
      opened.length = 0;
      el.dispatchEvent(new c.window.MouseEvent(type, { bubbles: true, cancelable: true, ...init }));
      return opened.slice();
    };
    if (cell) {
      out.opened_by.ctrl = fire(cell, 'click', { button: 0, ctrlKey: true });
      out.opened_by.meta = fire(cell, 'click', { button: 0, metaKey: true });
      out.opened_by.middle = fire(cell, 'auxclick', { button: 1 });
    }
    const box = row.querySelector('input[type=checkbox]');
    if (box) out.opened_by.ctrl_on_checkbox = fire(box, 'click', { button: 0, ctrlKey: true });
    const title = row.querySelector('.a-ptitle, .a-name');
    out.title_tag = title ? title.tagName : null;
    out.title_href = title ? title.getAttribute('href') : null;
    out.errors = c.errors;
    return out;
  };
  obs.airings = await probe('http://localhost:5000/channels?grain=airings', '.arow-air');
  obs.channels = await probe('http://localhost:5000/channels', '.arow');
  obs.errors = [...obs.airings.errors, ...obs.channels.errors];
  return obs;
}

/* ── 9. A `when` filter, and what parking one does to it ───────────────── */
async function whenScenario() {
  const obs = {};
  const c = boot({ url: 'http://localhost:5000/channels?grain=airings' });
  await c.settle();
  obs.grain_from_url = (c.params().grain || [])[0];

  // The rail leads with `when` on this grain: a dimension that exists only on the grain
  // you just entered is the reason you entered it.
  obs.rail_first_facet = (c.$('#rail .fac') || {}).dataset
    ? c.$('#rail .fac').dataset.fac : '';
  obs.rail_facets = c.$$('#rail .fac').map((f) => f.dataset.fac);

  // The three fixed values come from the catalog; the two fill-ins are the page's own.
  obs.when_values = c.$$('#rail [data-fac="when"] .prow[data-value]').map((r) => r.dataset.value);
  obs.when_fill_controls = c.$$('#rail [data-fac="when"] .when-fill').length;
  obs.rel_row_off_before_typing = !c.$('#rail [data-fac="when"] .when-fill.on');

  // Typing a number spells `next:<n>:<unit>` into the ONE filter value.
  const relBox = () => c.$('#rail [data-whenrel="n"]');
  // .focus() for real, not the FocusEvent helper: jsdom only moves activeElement for the
  // method, and activeElement is exactly what the rail's focus restore reads.
  if (relBox()) { relBox().focus(); c.type(relBox(), '3'); await c.settle(); }
  obs.when_after_rel = c.params()['f.when'] || [];
  // The rail is rewritten by that apply, so the box is re-queried rather than reused -
  // and the page has to have put the caret back in it, which is what this checks.
  obs.rel_box_kept_focus = c.document.activeElement === relBox();
  obs.rel_box_value_after_apply = (relBox() || {}).value;

  // Retyping REPLACES it rather than adding a second window.
  if (relBox()) { c.type(relBox(), '5'); await c.settle(); }
  obs.when_after_retype = c.params()['f.when'] || [];

  // Now flip to Channels: `when` is not a channel dimension, so the chip is PARKED -
  // visible, struck through, and not in the request.
  c.click(c.$('#grainslot-panel [data-grain="channels"]'));
  await c.settle();
  obs.when_sent_on_channels = c.params()['f.when'] || [];
  obs.parked_chip_visible = !!c.$('#wellbody .cs-chip.parked');
  obs.parked_chip_text = (c.$('#wellbody .cs-chip.parked') || {}).textContent || '';
  obs.no_toast_on_flip = c.toasts.slice();

  // ...and flipping back un-parks it, restoring both the chip and its controls.
  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  await c.settle();
  obs.when_after_flip_back = c.params()['f.when'] || [];
  obs.parked_chip_gone = !c.$('#wellbody .cs-chip.parked');
  obs.rel_control_value = (c.$('#rail [data-whenrel="n"]') || {}).value;

  /* THE CARET, simulated and observed through a SPY, because neither engine lets it be
     read off the element: Chrome raises InvalidStateError on `selectionStart` for
     `type=number`, and jsdom returns null. Both land on the same branch in the page, and
     the bug a real browser showed was that this branch defaulted the caret to 0 - so the
     next digit typed landed BEFORE the previous one and "36" became "63"
     (dev/changelog/414). What is assertable is where the restore asks for the caret. */
  const setCalls = [];
  const proto = c.window.HTMLInputElement.prototype;
  const realSet = proto.setSelectionRange;
  proto.setSelectionRange = function (a, b) {
    setCalls.push({ value: String(this.value), at: a });
    try { return realSet.call(this, a, b); } catch (e) { return undefined; }
  };
  if (relBox()) {
    relBox().focus();
    c.type(relBox(), '12');
    await c.settle();
    const last = setCalls[setCalls.length - 1] || null;
    obs.caret_set_at = last ? last.at : null;
    obs.caret_set_on_value_len = last ? last.value.length : null;
    obs.caret_box_still_focused = c.document.activeElement === relBox();
  }
  proto.setSelectionRange = realSet;

  obs.errors = c.errors;
  return obs;
}

/* ── 10. The airing grain at 375px: cards, not a table ─────────────────── */
async function airingMobileScenario() {
  const obs = {};
  const c = boot({ width: 375, url: 'http://localhost:5000/channels?grain=airings' });
  await c.settle();
  obs.strip_tabs = c.$$('#grainslot-panel .grainpill').map((b) => b.textContent.trim());
  obs.cards = c.$$('#ch-list .acard').length;
  obs.no_table_rows = c.$$('#ch-list .arow').length === 0;
  obs.card_title = (c.$('#ch-list .acard .ac-title') || {}).textContent || '';
  // P4, kebab only: the card carries the STATE as a badge and no Record button.
  obs.card_has_no_record_button = !c.$('#ch-list .acard [data-act^="rec-"]');
  obs.card_kebabs = c.$$('#ch-list .acard .cc-kebab').length;
  obs.card_kebab_carries_airing = c.$$('#ch-list .acard .cc-kebab')
    .every((k) => !!k.dataset.air);

  // The kebab sheet leads with the record action and names the showing.
  c.click(c.$('#ch-list .acard .cc-kebab'));
  await c.wait(30);
  const sheet = c.$('.cs-sheet');
  obs.sheet_opened = !!sheet;
  if (sheet) {
    obs.sheet_title = (c.$('.cs-sheet .modal-head h2') || {}).textContent || '';
    obs.sheet_rows = c.$$('.cs-sheet .prow .pv').map((el) => el.textContent.trim());
  }
  obs.errors = c.errors;
  return obs;
}

/* ── 11. The loading state: first paint, and a slow SAME-grain request ─── */
/* Two halves of one boot, because they are two halves of one rule (§12.2 of
   DESIGN-channel-search.md): nothing fetched yet puts the spinner up with no delay,
   and rows that are merely stale keep the floor until SLOW_RESULTS_MS. The 100ms
   observation is the flicker guard - it is what proves the delay exists at all. */
async function loadingScenario() {
  const obs = {};
  const c = boot({ hold: 'channels' });
  // Long enough for the catalog to land and the first rows request to go out and be
  // parked, and far short of the 600ms timer, which is not armed on a first paint.
  await c.wait(120);
  obs.first_paint_held = c.held();
  obs.first_paint_loading = c.loading();
  obs.first_paint_rows = c.rowCount();
  obs.first_paint_empty_shown = c.emptyShown();
  obs.first_paint_label = c.$('#all-loading-lbl').textContent;
  // The tab strip is drawn before any rows exist: it says which grain is loading.
  obs.first_paint_tabs = c.$$('#grainslot-panel .grainpill').map((b) => b.textContent.trim());
  obs.first_paint_count_line = c.$('#cs-count').textContent;
  obs.first_paint_pager = c.$('#pager').innerHTML;
  obs.first_paint_head_count = c.$('#all-head-count').textContent;

  c.release();
  await c.settle();
  obs.after_release_loading = c.loading();
  obs.after_release_rows = c.rowCount();

  // Now a SAME-grain request over the same held channel: the rows in hand are the
  // right shape, so they must survive the debounce and the first half-second.
  c.type(c.bar(), 'espn');
  await c.wait(400);              // 250ms debounce spent, ~150ms into the 600ms window
  obs.slow_early_rows = c.rowCount();
  obs.slow_early_loading = c.loading();
  // The spinner is armed by the page's own timers: 250ms debounce, then SLOW_RESULTS_MS
  // (600ms), so it lands 850ms after the keystroke. This observation is a poll rather than
  // a sleep to 900ms - the early one above can stay a flat sleep because starvation only
  // ever delays, which pushes it further INTO the window it is asserting about.
  obs.slow_late_waited_ms = await c.waitUntil(() => c.loading());
  obs.slow_late_rows = c.rowCount();
  obs.slow_late_loading = c.loading();
  obs.slow_late_count_line = c.$('#cs-count').textContent;
  obs.slow_late_scount = c.$('#scount').textContent;

  c.release();
  await c.settle();
  obs.slow_after_release_loading = c.loading();
  obs.slow_after_release_rows = c.rowCount();
  obs.errors = c.errors;
  return obs;
}

/* ── 12. Flipping the grain must not leave the other grain's rows up ───── */
async function loadingFlipScenario() {
  const obs = {};
  const c = boot();
  await c.settle();
  obs.before_rows = c.rowCount();
  obs.before_loading = c.loading();
  // Glyph-stripped like every other head-label capture in this file: the pinned
  // Channel column now carries the default sort's ▲, which is not a label.
  obs.before_head_labels = c.$$('#ahead > div')
    .map((d) => d.textContent.replace(/[▲▼]/g, '').trim());

  // Every airing request from here on is parked, so the flip can be observed mid-flight.
  c.setHold('airings');
  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  // 20ms: inside the debounce-free flip path and nowhere near the 600ms timer, so a
  // spinner here can only have come from the wrong-shape rule.
  await c.wait(20);
  obs.during_rows = c.rowCount();
  obs.during_loading = c.loading();
  obs.during_label = c.$('#all-loading-lbl').textContent;
  obs.during_empty_shown = c.emptyShown();
  obs.during_pager = c.$('#pager').innerHTML;
  obs.during_head_count = c.$('#all-head-count').textContent;
  obs.during_count_line = c.$('#cs-count').textContent;
  // The header has already become the airing grain's - which is exactly why the
  // channel rows underneath it were a mismatch and not merely stale.
  obs.during_head_labels = c.$$('#ahead > div').map((d) => d.textContent.trim());
  obs.during_active_tab = (c.$('#grainslot-panel .grainpill.on') || {}).textContent;

  await c.wait(80);
  c.release();
  await c.settle();
  obs.after_loading = c.loading();
  obs.after_airing_rows = c.$$('#ch-list .arow-air').length;
  obs.after_channel_rows = c.$$('#ch-list .arow:not(.arow-air)').length;
  obs.errors = c.errors;
  return obs;
}

/* ── 13. A request that never answers must not spin forever ────────────── */
async function loadingFailScenario() {
  const obs = {};
  const c = boot();
  await c.settle();
  c.setHold('airings');
  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  await c.wait(20);
  obs.loading_before_reject = c.loading();

  c.rejectHeld('the engine said no');
  await c.settle();
  obs.loading_after_reject = c.loading();
  obs.rows_after_reject = c.rowCount();
  obs.empty_shown = c.emptyShown();
  obs.empty_text = c.$('#all-empty').textContent;
  obs.toasts = c.toasts;
  obs.count_line = c.$('#cs-count').textContent;
  obs.errors = c.errors;
  return obs;
}

/* ── 14. A superseded request is CANCELLED, not merely ignored ─────────── */
/* The seq guard drops the stale answer; on 2026-08-01 the request behind it kept
   scanning 1.9M rows anyway, and ten of those at once exhausted the connection pool and
   killed a sync (dev/changelog/417). Every request is parked here, so all three
   generations are in flight at once and "which one was called off" is observable. */
async function supersededScenario() {
  const obs = {};
  const c = boot({ hold: 'channels' });
  await c.wait(120);                 // catalog answered; first rows + facets parked
  c.type(c.bar(), 'sup');
  await c.wait(300);                 // 250ms debounce spent: generation 2 is out
  c.type(c.bar(), 'supe');
  await c.wait(300);                 // generation 3 is out; 1 and 2 are superseded
  await c.settle();

  const searches = c.sent.filter((s) => s.url.includes('/api/channels/search')
    && !s.url.includes('/catalog') && !s.url.includes('/counts'));
  obs.requests = searches.map((s) => {
    const p = new c.window.URLSearchParams(s.url.split('?')[1] || '');
    return {
      q: p.get('q') || '',
      // `facets=` present-but-empty is the ROWS request; a non-empty list is the rail's.
      kind: (p.get('facets') || '') === '' ? 'rows' : 'facets',
      has_signal: !!s.signal,
      aborted: !!s.aborted,
    };
  });
  obs.held = c.held();
  obs.toasts = c.toasts;
  obs.empty_shown = c.emptyShown();
  obs.loading = c.loading();
  obs.errors = c.errors;
  return obs;
}

/* ── The proactive degraded-search notice ────────────────────────────────
   Driven by calling window.__applySearchReadiness directly rather than by faking a
   nav-status response: the harness evaluates util.js and channel-search.js only, and
   base.html's inline poll (which is what calls the hook in the real page) never runs
   here, nor would jsdom fetch it. That is the right seam anyway - the one line in
   base.html is verified by reading it, and what needs a DOM is the page's own
   behaviour. dev/changelog/427. */
async function readinessScenario() {
  const obs = {};
  const c = boot();
  await c.settle();
  /* Both readers tolerate the notice not existing at all, so that a page missing the
     element or the hook reports FALSE per observation rather than throwing and taking the
     whole class down as one setUpClass error - which would prove nothing about which
     assertion guards what. */
  const bar = () => c.$('#cs-degraded');
  const shown = () => { const el = bar(); return !!el && el.style.display !== 'none'; };
  const apply = (p) => {
    if (typeof c.window.__applySearchReadiness === 'function') c.window.__applySearchReadiness(p);
  };

  // Served HIDDEN and left that way by a page whose JS ran but was never told anything -
  // the nothing-active state has to survive boot on its own.
  obs.hidden_before_any_payload = !shown();
  obs.hook_registered = typeof c.window.__applySearchReadiness === 'function';

  const READY = { channels: { ready: true, reason: '' }, programs: { ready: true, reason: '' } };
  const PROGRAMS_STALE = {
    channels: { ready: true, reason: '' },
    programs: { ready: false, reason: 'the programs search index is stale - its source has changed' },
  };

  apply(READY);
  obs.hidden_when_ready = !shown();

  /* THE FIELD-SET CHOICE COMES FIRST NOW, because it decides whether a stale `programs`
     index is this search's problem at all. The channel grain's default scope is the
     channel's NAME alone (dev/changelog/860), which touches the channel index and nothing
     else - so the page opens in the state that must stay SILENT under PROGRAMS_STALE, and
     every "the notice is up" observation below has to put a program field in scope first. */
  apply(PROGRAMS_STALE);
  obs.hidden_for_default_channel_scope = !shown();
  obs.fields_at_default = c.params().in || [];
  // The scope switches live in the suggestion menu, which is built on focus - so there is
  // nothing to tick until the box has it.
  c.focus(c.bar());
  const epgTitle = c.$('#sugg input[data-field="epg-title"]');
  obs.found_epg_switch = !!epgTitle;
  if (epgTitle) { epgTitle.checked = true; c.change(epgTitle); }
  await c.wait(30);
  obs.fields_after_tick = c.params().in || [];
  obs.shown_with_a_program_field = shown();
  obs.shown_when_degraded = shown();
  obs.degraded_html = bar().innerHTML;

  // ...and it takes itself back down when the sync ends, without a reload.
  apply(READY);
  obs.hidden_after_flip_back = !shown();
  apply(PROGRAMS_STALE);

  /* And back off again, to the channel-only scope. Re-queried rather than reusing the node
     above - setField re-renders the whole menu, so the switch that was clicked is detached
     and a change event on it reaches no listener. */
  const epgTitleOff = c.$('#sugg input[data-field="epg-title"]');
  if (epgTitleOff) { epgTitleOff.checked = false; c.change(epgTitleOff); }
  await c.wait(30);
  obs.fields_after_untick = c.params().in || [];
  obs.hidden_for_channels_only_search = !shown();

  /* Back on once more: still no poll has happened between these states, so re-rendering on
     poll alone would leave the page silent until the next tick. */
  const epgTitle2 = c.$('#sugg input[data-field="epg-title"]');
  if (epgTitle2) { epgTitle2.checked = true; c.change(epgTitle2); }
  await c.wait(30);
  obs.shown_again_after_reticking = shown();

  // A degraded CHANNELS index warns whatever is being searched - it is under everything.
  apply({
    channels: { ready: false, reason: 'the channels search index has never been built' },
    programs: { ready: false, reason: 'the channels search index has never been built' },
  });
  obs.shown_when_channels_index_bad = shown();

  // The unindexed BADGE is a different region with a different trigger and must be
  // untouched by any of this (dev/changelog/418).
  obs.count_html = c.$('#cs-count').innerHTML;
  obs.errors = c.errors;
  obs.toasts = c.toasts;
  return obs;
}

/* ── The airing grain's row/counts split (dev/changelog/598) ─────────────
   The row query and the standing-breakdown count are answered by two separate requests
   on the airing grain now - `holdCounts` parks only the second one, so "rows are on
   screen, the total is still pending" is an observable state rather than something that
   only ever exists for one microtask under a synchronous stub. */
async function countsSplitScenario() {
  const obs = {};
  const c = boot({ holdCounts: true });
  await c.settle();
  c.click(c.$('#grainslot-panel [data-grain="airings"]'));
  await c.settle();

  const rowReq = c.sent.find((s) => s.url.includes('/api/channels/search?')
    && s.url.includes('grain=airings'));
  obs.row_request_asked_to_skip_counts = !!(rowReq && rowReq.url.includes('counts=0'));
  obs.counts_request_made = c.sent.some((s) => s.url.includes('/api/channels/search/counts')
    && s.url.includes('grain=airings'));

  /* The page tells the server which of ITS requests each one is, so a superseded scan can
     be cancelled instead of running to completion for an answer nobody will read
     (dev/changelog/678). `/counts` shares the ROW request's seq deliberately: those numbers
     describe that specific row response, so a newer ROW request is what invalidates them. */
  const q = (url) => new URLSearchParams((url || '').split('?')[1] || '');
  const countsReq = c.sent.find((s) => s.url.includes('/api/channels/search/counts')
    && s.url.includes('grain=airings'));
  const facetsReq = c.sent.find((s) => s.url.includes('/api/channels/search/facets'));
  obs.row_request_sid = q(rowReq && rowReq.url).get('sid');
  obs.row_request_seq = q(rowReq && rowReq.url).get('seq');
  obs.counts_request_sid = q(countsReq && countsReq.url).get('sid');
  obs.counts_request_seq = q(countsReq && countsReq.url).get('seq');
  obs.facets_request_seq = q(facetsReq && facetsReq.url).get('seq');
  // Every request of one page load carries the same id, or the server cannot tell two of
  // its requests apart from two different pages'.
  obs.every_request_shares_one_sid = c.sent
    .filter((s) => s.url.includes('/api/channels/search'))
    .map((s) => q(s.url).get('sid'))
    .filter((v) => v !== null)
    .every((v, _i, all) => v === all[0]);
  // Monotonic, per lane: a seq that repeated or went backwards would either cancel nothing
  // or cancel the live request.
  obs.row_seqs_in_order = c.sent
    .filter((s) => s.url.includes('/api/channels/search?'))
    .map((s) => Number(q(s.url).get('seq')))
    .every((v, i, all) => i === 0 || v > all[i - 1]);

  obs.counts_held_count = c.held();
  obs.rows_shown_while_counts_pending = c.$$('#ch-list .arow-air').length;
  obs.empty_shown_while_pending = c.emptyShown();

  // Nothing here may claim a wrong number while the total is still unknown - the honest
  // options are a real partial number, or no number, never a fabricated zero.
  obs.head_count_while_pending = (c.$('#all-head-count') || {}).textContent;
  obs.pager_html_while_pending = (c.$('#pager') || {}).innerHTML;
  obs.count_line_while_pending = (c.$('#cs-count') || {}).innerHTML;
  obs.in_box_count_while_pending = (c.$('#scount') || {}).textContent;

  c.release();
  await c.settle();

  obs.head_count_after = (c.$('#all-head-count') || {}).textContent;
  obs.count_line_after = (c.$('#cs-count') || {}).innerHTML;
  obs.errors = c.errors;
  return obs;
}

/* Same split, but on the CHANNEL grain: the breakdown there is already cheap
   (DESIGN-channel-search.md §10), so it stays bundled with the row response rather than
   paying a second round trip for no benefit - this pins that the split is airing-grain
   only, not a blanket behavior change. */
async function countsSplitChannelGrainScenario() {
  const obs = {};
  const c = boot();
  await c.settle();
  const rowReq = c.sent.find((s) => s.url.includes('/api/channels/search?'));
  obs.row_request_did_not_skip_counts = !!(rowReq && !rowReq.url.includes('counts=0'));
  obs.counts_request_made = c.sent.some((s) => s.url.includes('/api/channels/search/counts'));
  obs.head_count = (c.$('#all-head-count') || {}).textContent;
  obs.errors = c.errors;
  return obs;
}

/* ── 19. A declined aggregate is labeled with the SERVER'S reason ────────
   Two different things make the numbers too expensive to have - the index is unusable
   right now, or the index cannot answer this search at all - and they read differently
   because their remedies are different (wait vs. change a control). The page used to
   hardcode the first, so the second one's copy was simply wrong (dev/changelog/681). */
const DECLINE_WHY = 'showings that have already ended are not in the search index, and '
  + '"Show airings that have ended" is on';

async function declinedNumbersScenario() {
  const obs = {};
  const c = boot({ declineWhy: DECLINE_WHY });
  await c.settle();
  obs.row_count = c.rowCount();
  // #cs-count is renderCount()'s region - the "Showing these 100 ..." line under the bar.
  // #all-head-count above it is the plain "N channels" heading and carries no decline.
  obs.count_line_html = (c.$('#cs-count') || {}).innerHTML || '';
  const note = c.$('#rail .rnote');
  obs.rail_note_text = note ? note.textContent.trim() : '';
  obs.rail_note_tip = note ? (note.getAttribute('data-tip') || '') : '';
  const uncounted = c.$('#cs-count .uncounted');
  obs.count_tip = uncounted ? (uncounted.getAttribute('data-tip') || '') : '';
  obs.errors = c.errors;
  obs.toasts = c.toasts;
  return obs;
}

async function servedNumbersScenario() {
  /* The same observations with nothing declined, so "the note says X" cannot pass by
     virtue of the note always being there. */
  const obs = {};
  const c = boot();
  await c.settle();
  obs.rail_note_present = !!c.$('#rail .rnote');
  obs.count_uncounted_present = !!c.$('#cs-count .uncounted');
  obs.errors = c.errors;
  return obs;
}

const run = async () => {
  // The registries themselves, so the Python side compares the page against the catalog
  // it was served rather than against a re-typed list of fields, dimensions and sorts.
  /* The CHANNEL grain's visible dimensions, in that grain's rail order - not every
     dimension in the registry. Since the airing grain shipped, `when` is in the registry
     and is deliberately NOT offered on the channel grain, so comparing the page against
     the whole registry would demand it render a facet it cannot evaluate. */
  {
    const byKey = Object.fromEntries(CATALOG.dimensions.map((d) => [d.key, d]));
    out.catalog_dimensions = (CATALOG.by_grain.channels.dimensions || [])
      .map((k) => byKey[k]).filter((d) => d && !d.hidden);
  }
  out.catalog_fields = CATALOG.fields;
  out.catalog_sorts = CATALOG.sorts;
  out.boot = await bootScenario();
  out.box = await boxScenario();
  out.suggestion_facets = await suggestionFacetScenario();
  out.table = await tableScenario();
  out.name_col = await nameColScenario();
  out.saved = await savedScenario();
  out.page_size = await pageSizeScenario();
  out.context = await contextScenario();
  out.guide_action = await guideActionScenario();
  out.mobile = await mobileScenario();
  out.mobile_sheets = await mobileSheetsScenario();
  out.airing = await airingScenario();
  out.nav_click = await navClickScenario();
  out.when = await whenScenario();
  out.airing_mobile = await airingMobileScenario();
  out.loading = await loadingScenario();
  out.loading_flip = await loadingFlipScenario();
  out.loading_fail = await loadingFailScenario();
  out.superseded = await supersededScenario();
  out.readiness = await readinessScenario();
  out.counts_split = await countsSplitScenario();
  out.counts_split_channels = await countsSplitChannelGrainScenario();
  out.declined_numbers = await declinedNumbersScenario();
  out.served_numbers = await servedNumbersScenario();
  out.standing_inversion = await standingInversionScenario();
  out.decline_why = DECLINE_WHY;
  out.catalog_by_grain = CATALOG.by_grain;
  out.catalog_when_values = CATALOG.when_values;
  out.airing_rows = ROWS_AIRINGS.rows;
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
};

run().catch((e) => {
  process.stdout.write(JSON.stringify({ harness_error: `${e && e.stack ? e.stack : e}` }));
  process.exit(3);
});
