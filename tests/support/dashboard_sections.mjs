/* Drives the REAL Live Dashboard in jsdom and prints what it observed as JSON. Called by
   tests/test_dashboard_section_refresh_js.py, which owns every assertion - this file only
   reports, so a failure reads as "the page did X" in Python rather than as a node exit
   code. Same shape as tests/support/accounts_page.mjs.

   What it is for: the page keeps its sections current by swapping in a fresh server render
   when base.html's /api/nav-status poll hands them a trigger - the accounts and health
   sections (dev/changelog/1082), and the recording regions: the header count, the tiles,
   the timeline blob and the two record sections (dev/changelog/1143). None of that is reachable from Python - a page whose hooks never
   registered renders exactly the same markup, and a page that starts two health-check
   polls against one card renders the same markup too.

   What is real here and what is not: every page and every nav-status payload is what the
   Flask app really answered at each database state, base.html's own inline poll is what
   calls the hooks, and util.js + dashboard.js are the shipped files evaluated the way
   their <script src> would have been. Faked: the network, which answers from whichever
   state the scenario says the server is in; EventSource, which a scenario pushes frames
   through by hand; and the health widget's own 3s/1s timers, shortened so a scenario can step them.
   jsdom computes no layout and implements no navigation - a reload attempt is observed as
   jsdom's "Not implemented: navigation" report.

   argv: <fixture dir> <repo root>. The fixture dir holds <state>.html and <state>.json for
   each state in STATE_NAMES, plus active-run.json keyed by state. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATE_NAMES = ['idle', 'syncing', 'synced', 'checking', 'checking2',
                     'scheduled', 'started', 'finished'];
const STATES = {};
for (const s of STATE_NAMES) {
  STATES[s] = { html: read(`${s}.html`), nav: JSON.parse(read(`${s}.json`)) };
}
// What /api/channel-tests/active-run answers per state - the endpoint the health card's
// own 3s poll asks, which is a different question from the nav payload's task list.
const ACTIVE_RUN = JSON.parse(read('active-run.json'));

/* The page's own <script src> tags, replaced by the file's contents IN PLACE. jsdom does
   not fetch subresources, so the alternative is appending the scripts after construction -
   and that puts dashboard.js's DOMContentLoaded listener on the document AFTER jsdom has
   already fired the event, so the scenario has to dispatch a second one by hand and
   initDashboard runs twice. Two listeners on every sortable header is not a fair test of
   anything (one click sorted twice). Inlining keeps the real order: the scripts run during
   the parse, and the one real DOMContentLoaded initializes the page once.
   A tag whose file is not on disk is dropped, exactly as a page with no network has it. */
const inlineScripts = (html) => html.replace(
  /<script src="[^"]*\/static\/js\/([\w.-]+\.js)[^"]*"><\/script>/g,
  (tag, name) => {
    const path = `${REPO}/static/js/${name}`;
    return fs.existsSync(path) ? `<script>${fs.readFileSync(path, 'utf8')}</script>` : '';
  });

function boot(start, { serve = start } = {}) {
  const errors = [];
  const navigations = [];
  const warnings = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  vc.on('warn', (...a) => warnings.push(a.map(String).join(' ')));

  const server = { state: serve, pageStatus: 200 };
  const sent = [];
  const sources = [];
  /* How many health-check polls are in flight AT ONCE, which is how a scenario counts
     poll chains without counting requests per millisecond. A rate is the obvious measure
     and it is a bad one: every chain here runs at the squashed timer's ceiling, so the
     reading that moves under load is the BASELINE, and the comparison then fails on a
     slow machine rather than on a second chain.
     With `hold` on, the endpoint never answers. Each live chain parks exactly one request
     on it and waits, so the in-flight count IS the number of chains. */
  const run = { inFlight: 0, peak: 0, hold: false };
  const fetchStub = (target) => {
    const url = new URL(String(target), 'http://localhost:5000/');
    sent.push(url.pathname);
    if (url.pathname === '/api/channel-tests/active-run') {
      const body = ACTIVE_RUN[server.state];
      const resp = {
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: () => Promise.resolve(body),
        text: () => Promise.resolve(JSON.stringify(body)),
      };
      if (!run.hold) return Promise.resolve(resp);
      run.inFlight += 1;
      run.peak = Math.max(run.peak, run.inFlight);
      return new Promise(() => {});  // never settles: the chain parks here
    }
    if (url.pathname === '/') {
      const status = server.pageStatus;
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        headers: { get: () => 'text/html' },
        text: () => Promise.resolve(status === 200 ? STATES[server.state].html : 'Server Error'),
        json: () => Promise.reject(new Error('not json')),
      });
    }
    const payload = url.pathname === '/api/nav-status' ? STATES[server.state].nav
      : { success: true };
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(inlineScripts(STATES[start].html), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.EventSource = class { constructor(url) { this.url = url; sources.push(this); } close() {} };
      w.matchMedia = () => ({
        media: '', matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
      // The health card's poll reschedules itself with setTimeout(3000), squashed so a
      // scenario can watch several turns inside one settle(). setInterval is deliberately
      // left alone: base.html's own nav poll is one, and speeding that up would fire
      // dozens of polls inside every settle() and make "an unchanged payload fetches
      // nothing" unmeasurable. Each scenario drives the poll by hand instead.
      const realSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, Math.min(ms || 0, 3), ...args);
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};

  const $ = (s) => document.querySelector(s);
  const sec = (key) => $(`.dash-sec[data-sec="${key}"]`);
  const c = {
    window, document, server, sent, errors, navigations, warnings, $, sec, run,
    // Park every live chain on the endpoint and count them. Returns once each chain has
    // had a turn to issue its request.
    countChains: async () => {
      run.inFlight = 0; run.peak = 0; run.hold = true;
      await c.settle(40);
      return run.peak;
    },
    settle: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    // One frame through the page's own /api/stream handler, as the server would send it.
    sse: async (frame) => {
      const stream = sources.find((x) => x.url === '/api/stream');
      if (!stream) throw new Error('dashboard.js opened no /api/stream EventSource');
      stream.onmessage({ data: JSON.stringify(frame) });
      await c.settle();
    },
    recSig: () => ($('.dash-pulse') || { dataset: {} }).dataset.recSig,
    rowIds: (key) => Array.from(document.querySelectorAll(`.dash-sec[data-sec="${key}"] .drow`))
      .map((r) => Number(r.dataset.id)),
    tile: (label) => {
      const t = Array.from(document.querySelectorAll('.dash-pulse .mtile'))
        .find((el) => el.querySelector('.m-k').textContent.trim() === label);
      return t ? t.querySelector('.m-v').textContent.trim() : null;
    },
    headerSub: () => ($('#dash-sub') || { textContent: null }).textContent.trim(),
    barClass: (rid) => {
      const bar = $(`#tl .tl-bar[data-id="${rid}"]`);
      return bar ? bar.className : null;
    },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === '/').length,
    runFetches: () => sent.filter((p) => p === '/api/channel-tests/active-run').length,
    syncSig: () => (sec('accounts') || { dataset: {} }).dataset.syncSig,
    hasCard: () => Boolean($('#hc-card')),
    hcName: () => ($('#hc-card .d-name') || { textContent: null }).textContent,
    badge: () => ($('#hc-status-badge') || { textContent: null }).textContent,
    hcCell: (id) => ($(`#${id}`) || { textContent: null }).textContent,
    acctStatuses: () => Array.from(document.querySelectorAll('.dash-sec[data-sec="accounts"] .drow'))
      .map((r) => `${r.dataset.name}:${r.dataset.status}`),
    acctNames: () => Array.from(document.querySelectorAll('.dash-sec[data-sec="accounts"] .drow'))
      .map((r) => r.dataset.name),
    secOrder: () => Array.from(document.querySelectorAll('#dash-sections .dash-sec'))
      .map((s) => s.dataset.sec),
    sortedCol: (key) => {
      const h = sec(key) && sec(key).querySelector('.dash-head .sortable.sorted');
      return h ? `${h.dataset.sort}:${h.querySelector('.sort-ind').textContent.trim()}` : null;
    },
    sortHeader: (key, col) => sec(key).querySelector(`.dash-head .sortable[data-sort="${col}"]`),
  };
  return c;
}

const out = {};
const record = async (name, fn) => {
  let c = null;
  try {
    out[name] = await fn((...a) => { c = boot(...a); return c; });
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  } finally {
    if (c) c.window.close();
  }
};

/* ── The page as it opens, with a sync running ───────────────────────── */
await record('boot', async (start) => {
  const c = start('syncing');
  await c.settle();
  return {
    errors: c.errors,
    accountHook: typeof c.window.__applyAccountSync,
    backgroundHook: typeof c.window.__applyBackgroundTasks,
    syncSig: c.syncSig(),
    navSig: STATES.syncing.nav.account_sync,
    statuses: c.acctStatuses(),
    // base.html's own first poll has already run both hooks against a page that agrees
    // with them, so this is "nothing changed means nothing is fetched".
    pageFetches: c.pageFetches(),
    hasCard: c.hasCard(),
    runFetches: c.runFetches(),
  };
});

/* ── The sync finishes while the page is open ────────────────────────── */
await record('sync_finished', async (start) => {
  const c = start('syncing');
  await c.settle();
  const before = c.sec('accounts');
  const otherBefore = c.sec('live');
  c.server.state = 'synced';
  await c.poll();
  const after = {
    errors: c.errors,
    pageFetches: c.pageFetches(),
    statuses: c.acctStatuses(),
    sameSection: c.sec('accounts') === before,
    otherSectionUntouched: c.sec('live') === otherBefore,
    syncSig: c.syncSig(),
    navSig: STATES.synced.nav.account_sync,
    order: c.secOrder(),
  };
  // Converged: the next poll carries the signature the swapped-in section was rendered at.
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  return after;
});

/* ── A sort the user picked survives the swap ────────────────────────── */
await record('sort_survives', async (start) => {
  const c = start('syncing');
  await c.settle();
  c.click(c.sortHeader('accounts', 'name'));
  const sortedBefore = c.sortedCol('accounts');
  const namesBefore = c.acctNames();
  c.server.state = 'synced';
  await c.poll();
  return {
    errors: c.errors,
    sortedBefore,
    namesBefore,
    sortedAfter: c.sortedCol('accounts'),
    namesAfter: c.acctNames(),
    // The swapped-in headers are live too, not just re-labelled.
    reversible: (() => {
      c.click(c.sortHeader('accounts', 'name'));
      return { sorted: c.sortedCol('accounts'), names: c.acctNames() };
    })(),
  };
});

/* ── An unsorted section stays in the server's order ─────────────────── */
await record('unsorted_stays', async (start) => {
  const c = start('syncing');
  await c.settle();
  const namesBefore = c.acctNames();
  c.server.state = 'synced';
  await c.poll();
  return { errors: c.errors, namesBefore, namesAfter: c.acctNames(),
           sorted: c.sortedCol('accounts') };
});

/* ── A health check starts after the page loaded ─────────────────────── */
await record('check_starts', async (start) => {
  const c = start('idle');
  await c.settle();
  const before = { hasCard: c.hasCard(), runFetches: c.runFetches(),
                   empty: Boolean(c.sec('health').querySelector('.dash-empty')) };
  c.server.state = 'checking';
  await c.poll();
  await c.settle();
  return {
    errors: c.errors,
    before,
    after: {
      hasCard: c.hasCard(),
      pageFetches: c.pageFetches(),
      badge: c.badge(),
      progress: c.hcCell('hc-progress-text'),
      results: c.hcCell('hc-results'),
      empty: Boolean(c.sec('health').querySelector('.dash-empty')),
    },
    // The card's own poll is running now, and it is the only one: several nav polls
    // later the request count is still climbing at one chain's rate, not two.
    pollingAfterStart: c.runFetches() > 0,
  };
});

/* ── Starting the widget again never adds a second poll chain ────────── */
// The direct probe of the contract every health swap depends on: the health region's swap
// re-enters startHealthCheckWidget on each swap, and a run that outlives one swap would
// otherwise leave two chains writing one card and asking the server twice as often.
await record('one_poll_chain', async (start) => {
  const c = start('checking');
  await c.settle(40);
  const polled = c.runFetches() > 0;
  for (let i = 0; i < 4; i += 1) c.window.startHealthCheckWidget();
  return {
    errors: c.errors,
    polled,
    chains: await c.countChains(),
    pageFetches: c.pageFetches(),
    hasCard: c.hasCard(),
  };
});

/* ── A second run starts before the poll noticed the first ended ─────── */
await record('run_changes', async (start) => {
  const c = start('checking');
  await c.settle(40);
  const first = { name: c.hcName(), progress: c.hcCell('hc-progress-text') };
  // Never through 'idle': the nav payload carries a health check at both readings, so the
  // background hook has no edge to react to and only the card's own poll can tell.
  c.server.state = 'checking2';
  await c.settle(80);
  const second = { name: c.hcName(), progress: c.hcCell('hc-progress-text') };
  return {
    errors: c.errors,
    first,
    second,
    pageFetches: c.pageFetches(),
    hasCard: c.hasCard(),
    // One chain still, adopting the swapped-in card rather than a second one beside it.
    chains: await c.countChains(),
  };
});

/* ── The run ends: the card's own poll notices first ─────────────────── */
await record('check_ends', async (start) => {
  const c = start('checking');
  await c.settle();
  const running = { hasCard: c.hasCard(), badge: c.badge() };
  // The server has finished the run. The nav payload has not been asked yet - only the
  // card's own 3s poll, which is what learns this ~15s before the nav poll could.
  c.server.state = 'idle';
  await c.settle(40);
  const ended = {
    hasCard: c.hasCard(),
    pageFetches: c.pageFetches(),
    empty: Boolean(c.sec('health').querySelector('.dash-empty')),
    navigations: c.navigations.length,
  };
  const stopped = c.runFetches();
  await c.settle(40);
  ended.pollStopped = c.runFetches() === stopped;
  // The nav poll then agrees with the page and asks for nothing more.
  await c.poll();
  ended.pageFetchesAfterNavPoll = c.pageFetches();
  return { errors: c.errors, running, ended };
});

/* ── A refresh that fails leaves the page as it was ──────────────────── */
await record('failed_refresh', async (start) => {
  const c = start('syncing');
  await c.settle();
  c.server.state = 'synced';
  c.server.pageStatus = 500;
  await c.poll();
  const failed = { statuses: c.acctStatuses(), syncSig: c.syncSig(),
                   pageFetches: c.pageFetches(), warnings: c.warnings.slice() };
  c.server.pageStatus = 200;
  await c.poll();
  return { errors: c.errors, failed,
           retried: { statuses: c.acctStatuses(), pageFetches: c.pageFetches() } };
});

/* ── Nothing changed, but the relative times have aged ───────────────── */
await record('aged', async (start) => {
  const c = start('synced');
  await c.settle();
  const fresh = c.pageFetches();
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => true });
  await c.poll();
  const hiddenTab = c.pageFetches();
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => false });
  await c.poll();
  const visibleTab = c.pageFetches();
  await c.poll();
  return { errors: c.errors, fresh, hiddenTab, visibleTab, rightAfter: c.pageFetches() };
});

/* ── A section hidden in Customize is not re-fetched on the tick ─────── */
await record('hidden_section', async (start) => {
  const c = start('synced');
  await c.settle();
  const secEl = c.sec('accounts');
  secEl.hidden = true;
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  await c.poll();
  // Asked of the NODE, not of the fetch count: the recording regions' own minute tick
  // fetches the page on this same poll (dev/changelog/1143), and the question is whether
  // the hidden section rode along in that swap.
  const whileHidden = { replaced: c.sec('accounts') !== secEl, pageFetches: c.pageFetches() };
  // A real change still reaches it, so unhiding never reveals a stale sync state.
  c.server.state = 'syncing';
  await c.poll();
  const onChange = { replaced: c.sec('accounts') !== secEl, statuses: c.acctStatuses() };
  return { errors: c.errors, whileHidden, onChange };
});

/* ═══ The recording regions (dev/changelog/1143) ═══════════════════════════
   scheduled -> started -> finished, with the accounts and health state held still so no
   other trigger fires underneath. REC holds the ids the Python side seeded. */
const REC = JSON.parse(read('recordings.json'));

/* ── The page as it opens ────────────────────────────────────────────── */
await record('rec_boot', async (start) => {
  const c = start('scheduled');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyRecordingSignature,
    recSig: c.recSig(),
    navSig: STATES.scheduled.nav.recording_signature,
    pageFetches: c.pageFetches(),
  };
});

/* ── A recording starts; the nav poll is the first to hear ───────────── */
await record('rec_started_by_poll', async (start) => {
  const c = start('scheduled');
  await c.settle();
  const acctBefore = c.sec('accounts');
  // Every node taken out of #dash-sections from here on. A browser drops the scroll offset
  // of anything inside a node it moves, so the timeline section must never be among them
  // (dev/docs/BUGS.md 2026-09-26 @ 09:07:32 PM) - jsdom has no layout to show the scroll
  // itself snapping back, but it does see the move that causes it.
  const removed = [];
  new c.window.MutationObserver((muts) => muts.forEach((m) => m.removedNodes.forEach((n) => {
    if (n.dataset && n.dataset.sec) removed.push(n.dataset.sec);
  }))).observe(c.$('#dash-sections'), { childList: true });
  const before = { live: c.rowIds('live'), upcoming: c.rowIds('upcoming'),
                   capturing: c.tile('Capturing now'), sub: c.headerSub(),
                   bar: c.barClass(REC.soon) };
  c.server.state = 'started';
  await c.poll();
  const after = {
    live: c.rowIds('live'), upcoming: c.rowIds('upcoming'),
    capturing: c.tile('Capturing now'), sub: c.headerSub(), bar: c.barClass(REC.soon),
    recSig: c.recSig(), navSig: STATES.started.nav.recording_signature,
    pageFetches: c.pageFetches(), accountsUntouched: c.sec('accounts') === acctBefore,
    removed: removed.slice(),
  };
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  return { errors: c.errors, navigations: c.navigations, before, after };
});

/* ── A recording starts; its own SSE frame is the first to hear ──────── */
await record('rec_started_by_sse', async (start) => {
  const c = start('scheduled');
  await c.settle();
  c.server.state = 'started';
  await c.sse({ recording_id: REC.soon, event: 'SEGMENT_STARTED',
                data: { status: 'IN_PROGRESS' } });
  return { errors: c.errors, navigations: c.navigations, pageFetches: c.pageFetches(),
           live: c.rowIds('live'), upcoming: c.rowIds('upcoming') };
});

/* ── A recording finishes ────────────────────────────────────────────── */
await record('rec_finished', async (start) => {
  const c = start('started');
  await c.settle();
  const before = c.rowIds('live');
  c.server.state = 'finished';
  await c.sse({ recording_id: REC.alpha, event: 'RECORDING_COMPLETE',
                data: { status: 'COMPLETED' } });
  return { errors: c.errors, navigations: c.navigations, before, after: c.rowIds('live'),
           capturing: c.tile('Capturing now') };
});

/* ── What the user chose survives the swap ───────────────────────────── */
await record('rec_user_state', async (start) => {
  const c = start('scheduled');
  await c.settle();
  c.click(c.sortHeader('live', 'name'));
  const sortedBefore = c.rowIds('live');
  // Hide the live section the way the Customize switch does it. The stub saves nothing, so
  // the server keeps rendering the section shown, and only the client's re-apply after the
  // swap can keep it hidden.
  const sw = c.document.createElement('input');
  sw.dataset.czOn = 'live';
  c.document.body.appendChild(sw);
  sw.dispatchEvent(new c.window.Event('change', { bubbles: true }));
  const liveHiddenBefore = c.sec('live').hidden;
  c.server.state = 'started';
  await c.poll();
  return {
    errors: c.errors, sortedBefore, sortedAfter: c.rowIds('live'),
    sortedCol: c.sortedCol('live'), liveHidden: c.sec('live').hidden,
    liveHiddenBefore,
  };
});

/* ── A Customize reorder still moves the sections ────────────────────── */
await record('rec_reorder', async (start) => {
  const c = start('scheduled');
  await c.settle();
  const before = c.secOrder();
  const up = c.document.createElement('button');
  up.dataset.czUp = 'live';
  c.document.body.appendChild(up);
  c.click(up);
  await c.settle();
  return { errors: c.errors, before, after: c.secOrder() };
});

/* ── Nothing changed, but "Starts in 5m" has aged ────────────────────── */
await record('rec_minute_tick', async (start) => {
  const c = start('scheduled');
  await c.settle();
  const pulse = c.$('.dash-pulse');
  const acct = c.sec('accounts');
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => true });
  await c.poll();
  const hiddenTab = { pageFetches: c.pageFetches(), replaced: c.$('.dash-pulse') !== pulse };
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => false });
  await c.poll();
  // One fetch carries both minute ticks - the accounts section's and this one.
  const visibleTab = { pageFetches: c.pageFetches(), replaced: c.$('.dash-pulse') !== pulse,
                       accountsReplaced: c.sec('accounts') !== acct };
  return { errors: c.errors, hiddenTab, visibleTab };
});

process.stdout.write(JSON.stringify(out));
