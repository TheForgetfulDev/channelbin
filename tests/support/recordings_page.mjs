/* Drives the REAL Recordings list in jsdom and prints what it observed as JSON. Called by
   tests/test_recordings_page_live_js.py, which owns every assertion - this file only
   reports. Same shape as tests/support/dashboard_sections.mjs.

   What it is for: the list keeps itself current (dev/changelog/1144) - a swap of the row
   set when /api/nav-status's recording_signature moves, a minute tick that rewrites the
   clock cells from /api/recordings/times, and the page's own actions refreshing instead
   of reloading. None of that is reachable from Python.

   What is real: every page, nav-status payload and times payload is what the Flask app
   answered at each database state; base.html's own inline poll calls the hook; util.js,
   filter-bar.js, guide.js and recordings.js are the shipped files, inlined where their
   <script src> tags sit. Faked: the network (answers from whichever state the scenario
   says the server is in), EventSource, and the clock where a scenario moves it. A reload
   is observed as jsdom's "Not implemented: navigation" report.

   argv: <fixture dir> <repo root>. The fixture dir holds <state>.html, <state>.json and
   <state>.times.json per state, and ids.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATE_NAMES = ['scheduled', 'started', 'finished', 'empty'];
const STATES = {};
for (const s of STATE_NAMES) {
  STATES[s] = { html: read(`${s}.html`), nav: JSON.parse(read(`${s}.json`)),
                times: JSON.parse(read(`${s}.times.json`)) };
}
const IDS = JSON.parse(read('ids.json'));

// duplicated from tests/support/dashboard_sections.mjs - each harness is run standalone by
// its own test module, and a shared import would couple two fixtures' lifetimes.
const inlineScripts = (html) => html.replace(
  /<script src="[^"]*\/static\/js\/([\w.-]+\.js)[^"]*"><\/script>/g,
  (tag, name) => {
    const path = `${REPO}/static/js/${name}`;
    return fs.existsSync(path) ? `<script>${fs.readFileSync(path, 'utf8')}</script>` : '';
  });

function boot(start) {
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

  const server = { state: start, times: null };
  const sent = [];
  const answer = (body, type = 'application/json') => Promise.resolve({
    ok: true, status: 200,
    headers: { get: () => type },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  });
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), 'http://localhost:5000/');
    sent.push(`${(opts.method || 'GET').toUpperCase()} ${url.pathname}`);
    if (url.pathname === '/recordings') return answer(STATES[server.state].html, 'text/html');
    if (url.pathname === '/api/nav-status') return answer(STATES[server.state].nav);
    if (url.pathname === '/api/recordings/times') {
      return answer(server.times || STATES[server.state].times);
    }
    return answer({ success: true });
  };

  const dom = new JSDOM(inlineScripts(STATES[start].html), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/recordings',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.EventSource = class { constructor(url) { this.url = url; } close() {} };
      w.matchMedia = () => ({
        media: '', matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  const $ = (s) => document.querySelector(s);
  const row = (id) => $(`.rec-list .rows .row[data-id="${id}"]`);
  const c = {
    window, document, server, errors, navigations, warnings, $,
    settle: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === 'GET /recordings').length,
    timesFetches: () => sent.filter((p) => p === 'GET /api/recordings/times').length,
    posts: () => sent.filter((p) => p.startsWith('POST ')),
    recSig: () => ($('#rec-sub') || { dataset: {} }).dataset.recSig,
    sub: () => ($('#rec-sub') || { textContent: '' }).textContent.trim(),
    sectionOf: (id) => {
      const r = row(id);
      return r ? r.closest('.rows').dataset.section : null;
    },
    acts: (id) => {
      const r = row(id);
      return r ? Array.from(r.querySelectorAll('[data-act]')).map((b) => b.dataset.act) : null;
    },
    visible: () => Array.from(document.querySelectorAll('.rec-list .rows .row'))
      .filter((r) => r.style.display !== 'none').map((r) => Number(r.dataset.id)),
    order: (section) => Array.from(document.querySelectorAll(
      `.rec-list .rows[data-section="${section}"] .row`)).map((r) => Number(r.dataset.id)),
    rel: (id) => {
      const r = row(id);
      const el = r && r.querySelector('[data-col="time"] .rel');
      return el ? el.textContent : null;
    },
    day: (id) => {
      const r = row(id);
      const el = r && r.querySelector('[data-col="time"] .day');
      return el ? el.textContent : null;
    },
    sortedCol: () => {
      const h = $('.list-head .sortable.sorted');
      return h ? `${h.dataset.sort}:${h.querySelector('.sort-ind').textContent.trim()}` : null;
    },
    chips: () => Array.from(document.querySelectorAll('#filter-chips .chip.active-filter'))
      .map((el) => el.textContent.trim()),
    row,
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

/* ── The page as it opens ────────────────────────────────────────────── */
await record('boot', async (start) => {
  const c = start('scheduled');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyRecordingSignature,
    recSig: c.recSig(), navSig: STATES.scheduled.nav.recording_signature,
    pageFetches: c.pageFetches(), timesFetches: c.timesFetches(),
    soonSection: c.sectionOf(IDS.soon), soonActs: c.acts(IDS.soon),
  };
});

/* ── A recording starts while the page is open ───────────────────────── */
await record('started', async (start) => {
  const c = start('scheduled');
  await c.settle();
  const subBefore = c.sub();
  c.server.state = 'started';
  await c.poll();
  const after = {
    soonSection: c.sectionOf(IDS.soon), soonActs: c.acts(IDS.soon),
    sub: c.sub(), subBefore, pageFetches: c.pageFetches(),
    recSig: c.recSig(), navSig: STATES.started.nav.recording_signature,
  };
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  return { errors: c.errors, navigations: c.navigations, after };
});

/* ── A recording finishes while the page is open ─────────────────────── */
await record('finished', async (start) => {
  const c = start('started');
  await c.settle();
  const before = { section: c.sectionOf(IDS.live), acts: c.acts(IDS.live) };
  c.server.state = 'finished';
  await c.poll();
  return { errors: c.errors, navigations: c.navigations, before,
           after: { section: c.sectionOf(IDS.live), acts: c.acts(IDS.live) } };
});

/* ── Search text and sort survive the swap ───────────────────────────── */
await record('search_and_sort', async (start) => {
  const c = start('scheduled');
  await c.settle();
  c.click(c.$('.list-head .sortable[data-sort="name"]'));
  const input = c.$('#rec-search');
  input.value = 'show';
  input.dispatchEvent(new c.window.Event('input', { bubbles: true }));
  const visibleBefore = c.visible();
  c.server.state = 'started';
  await c.poll();
  return {
    errors: c.errors, visibleBefore, visibleAfter: c.visible(),
    search: c.$('#rec-search').value, sortedCol: c.sortedCol(),
    liveOrder: c.order('live'),
  };
});

/* ── A filter chip survives the swap ─────────────────────────────────── */
await record('filter', async (start) => {
  const c = start('scheduled');
  await c.settle();
  c.click(c.$('#filter-chips [data-menu]'));
  c.click(c.$('#filter-menu [data-fdim="status"]'));
  c.click(c.$('#filter-menu [data-fval="COMPLETED"]'));
  const visibleBefore = c.visible();
  const chipsBefore = c.chips();
  c.server.state = 'finished';
  await c.poll();
  return { errors: c.errors, visibleBefore, chipsBefore,
           visibleAfter: c.visible(), chipsAfter: c.chips() };
});

/* ── An open row menu holds the swap ─────────────────────────────────── */
await record('menu_hold', async (start) => {
  const c = start('scheduled');
  await c.settle();
  c.click(c.row(IDS.soon).querySelector('[data-menu]'));
  const menuOpen = Boolean(c.row(IDS.soon).querySelector('.menu.open'));
  c.server.state = 'started';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), soonSection: c.sectionOf(IDS.soon),
                      stillOpen: Boolean(c.$('.rec-list .menu.open')) };
  c.document.dispatchEvent(new c.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await c.poll();
  return { errors: c.errors, menuOpen, whileOpen,
           afterClose: { pageFetches: c.pageFetches(), soonSection: c.sectionOf(IDS.soon) } };
});

/* ── The minute tick rewrites the clock cells, and only those ────────── */
await record('tick', async (start) => {
  const c = start('scheduled');
  await c.settle();
  const soonRow = c.row(IDS.soon);
  const relBefore = c.rel(IDS.soon);
  const times = JSON.parse(JSON.stringify(STATES.scheduled.times));
  times.rows[String(IDS.soon)] = { day: 'TICK-DAY', rel: 'TICK-REL' };
  c.server.times = times;
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => true });
  await c.poll();
  const hiddenTab = { timesFetches: c.timesFetches(), rel: c.rel(IDS.soon) };
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => false });
  await c.poll();
  const visibleTab = { timesFetches: c.timesFetches(), pageFetches: c.pageFetches(),
                       rel: c.rel(IDS.soon), day: c.day(IDS.soon),
                       sameRow: c.row(IDS.soon) === soonRow };
  await c.poll();
  return { errors: c.errors, relBefore, hiddenTab, visibleTab,
           timesFetchesRightAfter: c.timesFetches() };
});

/* ── The page's own action refreshes instead of reloading ────────────── */
await record('action', async (start) => {
  const c = start('started');
  await c.settle();
  c.click(c.row(IDS.done).querySelector('[data-act="delete"]'));
  await c.settle(20);
  // The newest dialog is the confirm; the page also carries the (hidden) shared record
  // modal, which has a danger button of its own.
  const confirm = Array.from(c.document.querySelectorAll('.btn-danger'))
    .filter((b) => b.textContent.trim() === 'Delete').pop();
  const hadConfirm = Boolean(confirm);
  if (confirm) c.click(confirm);
  await c.settle(120);
  return { errors: c.errors, navigations: c.navigations, hadConfirm, posts: c.posts(),
           pageFetches: c.pageFetches() };
});

/* ── Crossing the empty state reloads, in both directions ────────────── */
await record('first_recording', async (start) => {
  const c = start('empty');
  await c.settle();
  const before = c.navigations.length;
  c.server.state = 'scheduled';
  await c.poll();
  return { errors: c.errors, hook: typeof c.window.__applyRecordingSignature,
           before, after: c.navigations.length };
});

await record('last_recording', async (start) => {
  const c = start('finished');
  await c.settle();
  const before = c.navigations.length;
  c.server.state = 'empty';
  await c.poll();
  return { errors: c.errors, before, after: c.navigations.length,
           pageFetches: c.pageFetches() };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
