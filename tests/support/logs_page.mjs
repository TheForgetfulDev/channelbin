/* Drives the REAL /logs page in jsdom and prints what it observed as JSON.
   Called by tests/test_logs_page_js.py, which owns every assertion - this file only
   reports, so a failure reads as "the page did X" in Python rather than as a node
   exit code. Same shape as tests/support/channel_search_page.mjs.

   Why it exists: everything this page does is client side. The server renders an
   empty box and a status badge reading "Connecting", which is also exactly what a
   page whose JavaScript threw on line one looks like - to curl, and to every Python
   test that can only see the response body. The filter's one-writer guarantee, the
   counts, the severity classes and the phone sheet are unreachable from Python and
   are the parts most likely to be quietly wrong.

   What is real here and what is not: the markup is what the Flask route rendered,
   the history payload is what /api/logs/history really answered over a seeded log
   file, and util.js + logs.js are the shipped files evaluated into the window the
   way their <script src> would have been. Faked: the network, EventSource (jsdom
   has none), and the clipboard. jsdom computes no layout, so nothing about the box
   actually filling the viewport, the sticky bar or the 375px stack can be asked
   here - those need a browser.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and history.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGE = read('page.html');
const HISTORY = JSON.parse(read('history.json'));

const JS = ['util.js', 'logs.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot({ width = 1280, history = HISTORY } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const sent = [];
  const copied = [];
  const streams = [];

  const fetchStub = (target) => {
    const href = String(target);
    sent.push(href);
    const payload = href.includes('/api/logs/history') ? history : {};
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/logs',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      /* jsdom implements no EventSource at all. Recording the instances is the only
         way to ask whether Stop actually closed the stream rather than just
         relabelling the button - which is the half a status-badge restyle breaks. */
      w.EventSource = class {
        constructor(url) {
          this.url = url;
          this.closed = false;
          streams.push(this);
        }

        close() { this.closed = true; }
      };
      Object.defineProperty(w, 'innerWidth', { value: width, configurable: true });
      w.matchMedia = (query) => {
        const max = /max-width:\s*(\d+)px/.exec(query);
        const matches = max ? width <= Number(max[1]) : true;
        return {
          media: query, matches, onchange: null,
          addEventListener() {}, removeEventListener() {},
          addListener() {}, removeListener() {}, dispatchEvent() { return false; },
        };
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  Object.defineProperty(window.navigator, 'clipboard', {
    value: { writeText: (t) => { copied.push(t); return Promise.resolve(); } },
    configurable: true,
  });

  JS.forEach((src) => window.eval(src));
  const toasts = [];
  const realToast = window.showToast;
  window.showToast = (msg, opts) => { toasts.push(String(msg)); return realToast(msg, opts); };

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const ctx = {
    window, document, errors, sent, toasts, copied, streams, $, $$,
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    type: (el, v) => { el.value = v; el.dispatchEvent(new window.Event('input', { bubbles: true })); },
    wait: (ms) => new Promise((r) => setTimeout(r, ms)),
    // The history fetch is a promise chain; the search box debounces 150ms.
    settle: () => new Promise((r) => setTimeout(r, 220)),
    live: () => streams[streams.length - 1],
    open: () => { const s = ctx.live(); if (s && s.onopen) s.onopen(); },
    fail: () => { const s = ctx.live(); if (s && s.onerror) s.onerror(); },
    emit: (rec) => { const s = ctx.live(); s.onmessage({ data: JSON.stringify(rec) }); },
    rows: () => $$('#log-box .log-row'),
    visible: () => $$('#log-box .log-row').filter((r) => !r.hidden),
    rowAt: (i) => $$('#log-box .log-row')[i],
    text: (sel) => (($(sel) || {}).textContent || '').trim(),
    chip: (src) => $$('#src-chips .chip').find((c) => c.dataset.src === src),
    lvChip: (lv) => $$('#lv-chips .chip').find((c) => c.dataset.level === lv),
    status: () => ({ text: ctx.text('#log-status'), cls: $('#log-status').className }),
    // Where the filter toolbar currently lives, by id of its parent.
    filterHome: () => ($('#log-filters').parentElement || {}).id
      || ($('#log-filters').parentElement || {}).className,
  };
  return ctx;
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

/* ── The page as it opens ───────────────────────────────────────────── */
await record('boot', async () => {
  const c = boot();
  await c.settle();
  c.open();
  const werkzeug = c.rows().filter((r) => r.dataset.source === 'werkzeug');
  return {
    errors: c.errors,
    total: c.rows().length,
    visible: c.visible().length,
    // Off by default, so the rows exist and are hidden rather than being dropped.
    noisyRows: werkzeug.length,
    noisyHidden: werkzeug.every((r) => r.hidden),
    noisyChipOffered: !!c.chip('werkzeug'),
    noisyChipActive: c.chip('werkzeug').classList.contains('active'),
    count: c.text('#log-count'),
    status: c.status(),
    streamUrl: c.live().url,
    // Severity is the row's own class, never the level's.
    sev: c.rows().map((r) => [r.dataset.level, r.className]),
    chipCounts: c.$$('#src-chips .chip .cn').map((n) => [n.dataset.cn, n.textContent]),
    emptyGone: !c.$('#log-empty'),
  };
});

/* ── The filter toggles visibility and never rebuilds ───────────────── */
await record('filter', async () => {
  const c = boot();
  await c.settle();
  c.open();
  /* Tag the live nodes with a JS PROPERTY, not an attribute: an innerHTML rebuild
     round-trips every attribute back onto the new nodes, so a data-* marker would
     survive the very defect this is here to catch. Node identity is the only thing
     a rebuild cannot fake - and it is also what a live text selection is anchored
     to (BUGS.md 2026-07-18). */
  c.rows().forEach((r, i) => { r.__probe = i; });
  const before = c.visible().length;
  c.click(c.lvChip('INFO'));
  const afterInfoOff = c.visible().length;
  const tagsSurvived = c.rows().every((r, i) => r.__probe === i);
  const countAfter = c.text('#log-count');
  c.click(c.lvChip('INFO'));
  return {
    errors: c.errors,
    before,
    afterInfoOff,
    infoRows: c.rows().filter((r) => r.dataset.level === 'INFO').length,
    tagsSurvived,
    countAfter,
    restored: c.visible().length,
    // Hidden by the attribute, never by an inline style (style.css ships
    // `[hidden] { display: none !important }` for exactly this).
    inlineStyles: c.rows().filter((r) => r.getAttribute('style')).length,
  };
});

/* ── Search, and the clear x that 3.11 makes mandatory ──────────────── */
await record('search', async () => {
  const c = boot();
  await c.settle();
  c.open();
  c.type(c.$('#log-search'), 'stalled');
  await c.settle();
  const hits = c.visible().length;
  // Read BEFORE the clear below: everything in the returned object is evaluated at
  // return time, so a count captured there would be the post-clear one.
  const countWhileFiltered = c.text('#log-count');
  const wrapLit = c.$('#log-search-wrap').classList.contains('has-text');
  c.click(c.$('#log-search-clear'));
  await c.settle();
  return {
    errors: c.errors,
    hits,
    wrapLit,
    countWhileFiltered,
    afterClear: c.visible().length,
    inputAfterClear: c.$('#log-search').value,
    wrapLitAfterClear: c.$('#log-search-wrap').classList.contains('has-text'),
  };
});

/* ── A line arriving on the stream ──────────────────────────────────── */
await record('stream', async () => {
  const c = boot();
  await c.settle();
  c.open();
  const before = c.rows().length;
  c.emit({ ts: '2026-08-04 09:00:00,000', level: 'ERROR', source: 'app.recorder', message: 'capture died' });
  const afterKnown = c.rows().length;
  // A logger nobody declared must still become filterable rather than silently
  // joining the list under someone else's chip.
  c.emit({ ts: '2026-08-04 09:00:01,000', level: 'INFO', source: 'app.routes.brandnew', message: 'hello' });
  const last = c.rowAt(c.rows().length - 1);
  return {
    errors: c.errors,
    before,
    afterKnown,
    afterUnknown: c.rows().length,
    newRowClass: c.rowAt(before).className,
    recorderChipCount: c.chip('app.recorder').querySelector('.cn').textContent,
    unknownChipOffered: !!c.chip('app.routes.brandnew'),
    unknownChipLabel: c.chip('app.routes.brandnew').textContent.trim().split(/\s+/)[0],
    unknownRowVisible: !last.hidden,
    count: c.text('#log-count'),
  };
});

/* ── The status badge says what the stream is actually doing ────────── */
await record('status', async () => {
  const c = boot();
  const atBoot = c.status();
  await c.settle();
  const connecting = c.status();
  c.open();
  const live = c.status();
  const pulse = !!c.$('#log-status .pulse');
  c.fail();
  const reconnecting = c.status();
  c.open();
  c.click(c.$('#btn-live'));
  const stopped = c.status();
  return {
    errors: c.errors,
    atBoot,
    connecting,
    live,
    pulse,
    reconnecting,
    stopped,
    liveBtn: c.text('#btn-live'),
    streamClosed: c.streams.every((s) => s.closed),
    toasts: c.toasts,
  };
});

/* ── Clear and Copy ─────────────────────────────────────────────────── */
await record('clearAndCopy', async () => {
  const c = boot();
  await c.settle();
  c.open();
  c.click(c.lvChip('DEBUG'));         // hide something, so Copy has a choice to get wrong
  const visible = c.visible().length;
  c.click(c.$('#btn-copy'));
  await c.settle();
  const copiedLines = (c.copied[0] || '').split('\n');
  c.click(c.$('#btn-clear'));
  const afterClear = {
    rows: c.rows().length,
    count: c.text('#log-count'),
    chipCounts: c.$$('#src-chips .chip .cn').map((n) => n.textContent),
    empty: !!c.$('#log-box .log-empty'),
  };
  c.emit({ ts: '2026-08-04 09:00:02,000', level: 'INFO', source: 'app.recorder', message: 'after clear' });
  return {
    errors: c.errors,
    visible,
    copiedLines: copiedLines.length,
    // A multi-line entry (a traceback) is copied whole, so lines != rows. Rows are
    // the ones that open with a timestamp.
    copiedRows: copiedLines.filter((l) => /^\d\d:\d\d:\d\d/.test(l)).length,
    copiedHasDebug: /DEBUG/.test(c.copied[0] || ''),
    afterClear,
    rowsAfterEmit: c.rows().length,
    countAfterEmit: c.text('#log-count'),
    toasts: c.toasts,
  };
});

/* ── The phone sheet (16.5 item 4) ──────────────────────────────────── */
await record('sheet', async () => {
  const c = boot({ width: 375 });
  await c.settle();
  c.open();
  const homeBefore = c.filterHome();
  const summaryBefore = c.text('#lf-cur');
  c.click(c.$('#logfilter-btn'));
  const openState = {
    modals: c.$$('.modal').length,
    // The SAME node, moved - not a second copy with its own state.
    filtersInModal: !!c.$('.modal-body #log-filters'),
    chipSets: c.$$('#src-chips').length,
    title: c.text('.modal-head h2'),
  };
  // The moved toolbar has to still work, which is the thing a re-render would break.
  c.click(c.lvChip('INFO'));
  const filteredInSheet = c.visible().length;
  const summaryFiltered = c.text('#lf-cur');
  const barLit = c.$('#logfilter-btn').classList.contains('on');
  c.click(c.$$('.modal-foot .btn').find((b) => b.textContent === 'Reset'));
  const afterReset = { visible: c.visible().length, stillOpen: c.$$('.modal').length };
  c.click(c.$$('.modal-foot .btn').find((b) => b.textContent === 'Done'));
  return {
    errors: c.errors,
    homeBefore,
    summaryBefore,
    openState,
    filteredInSheet,
    summaryFiltered,
    barLit,
    afterReset,
    modalsAfterDone: c.$$('.modal').length,
    homeAfter: c.filterHome(),
    // Closing the sheet must not lose the filter, and it must still be wired.
    worksAfterClose: (() => {
      const before = c.visible().length;
      c.click(c.lvChip('DEBUG'));
      return before !== c.visible().length;
    })(),
    noteGone: !c.$('.lf-note'),
  };
});

/* ── An unreachable history file ────────────────────────────────────── */
await record('emptyHistory', async () => {
  const c = boot({ history: [] });
  await c.settle();
  c.open();
  const emptyText = c.text('#log-box .log-empty');
  c.emit({ ts: '2026-08-04 09:00:03,000', level: 'INFO', source: 'app.recorder', message: 'first line' });
  return {
    errors: c.errors,
    emptyText,
    rowsAfterEmit: c.rows().length,
    emptyGone: !c.$('#log-box .log-empty'),
    count: c.text('#log-count'),
  };
});

/* Explicit exit: jsdom's visual pretence and connectSSE's pending reconnect timer
   both keep node's event loop alive long after the last observation. */
process.stdout.write(JSON.stringify(out));
process.exit(0);
