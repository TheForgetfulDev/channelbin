/* Drives the REAL /accounts page in jsdom and prints what it observed as JSON.
   Called by tests/test_accounts_page_js.py, which owns every assertion - this file only
   reports, so a failure reads as "the list did X" in Python rather than as a node exit
   code. Same shape as tests/support/logs_page.mjs.

   What it is for: the list keeps itself current by swapping in a fresh server render when
   base.html's /api/nav-status poll hands it a sync signature that no longer matches the
   one its rows were rendered at (dev/changelog/921). None of that is reachable from
   Python - a page whose hook never registered renders exactly the same markup.

   What is real here and what is not: every page and every nav-status payload is what the
   Flask app really answered at three database states (no accounts; one account mid-sync;
   that sync finished), base.html's own inline poll is what calls the hook, and util.js +
   the three account scripts are the shipped files evaluated the way their <script src>
   would have been. Faked: the network, which answers from whichever state the scenario
   says the server is in. jsdom computes no layout and implements no navigation - a row
   click is observed as jsdom's "Not implemented: navigation" report, one per attempt.

   argv: <fixture dir> <repo root>. The fixture dir holds <state>.html and <state>.json
   for each of empty, syncing and done. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATES = {};
for (const s of ['empty', 'syncing', 'done']) {
  STATES[s] = { html: read(`${s}.html`), nav: JSON.parse(read(`${s}.json`)) };
}
const JS = ['util.js', 'account-modal.js', 'account-actions.js', 'accounts.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot(start, { serve = start, width = 1280 } = {}) {
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

  // What the "server" currently answers. A scenario moves it between states.
  const server = { state: serve, pageStatus: 200 };
  const sent = [];
  const fetchStub = (target) => {
    const url = new URL(String(target), 'http://localhost:5000/accounts');
    sent.push(url.pathname);
    if (url.pathname === '/accounts') {
      const body = STATES[server.state].html;
      const status = server.pageStatus;
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        headers: { get: () => 'text/html' },
        text: () => Promise.resolve(status === 200 ? body : 'Internal Server Error'),
        json: () => Promise.reject(new Error('not json')),
      });
    }
    const payload = url.pathname === '/api/nav-status' ? STATES[server.state].nav : {};
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(STATES[start].html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/accounts',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      Object.defineProperty(w, 'innerWidth', { value: width, configurable: true });
      w.matchMedia = (query) => {
        const max = /max-width:\s*(\d+)px/.exec(query);
        return {
          media: query, matches: max ? width <= Number(max[1]) : true, onchange: null,
          addEventListener() {}, removeEventListener() {},
          addListener() {}, removeListener() {}, dispatchEvent() { return false; },
        };
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const row = (name) => $(`.acct-rows .arow[data-name="${name}"]`);
  const c = {
    window, document, server, sent, errors, navigations, warnings, $, row,
    // base.html's poll is a fetch -> json -> hook chain, and the hook's swap is another
    // fetch -> text -> parse. Every stub resolves immediately, so one macrotask later
    // than all of that is enough.
    settle: () => new Promise((r) => setTimeout(r, 60)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === '/accounts').length,
    rowState: (name) => {
      const r = row(name);
      if (!r) return null;
      return {
        cls: r.className.trim().split(/\s+/),
        // Upper-cased here because CSS does it on screen: the badge text is "Syncing".
        status: r.querySelector('.a-status').textContent.trim().toUpperCase(),
        acts: Array.from(r.querySelectorAll('[data-act]')).map((b) => b.dataset.act),
      };
    },
    liveSig: () => ($('#acct-live') || { dataset: {} }).dataset.syncSig,
    total: () => ($('#acct-total') || { textContent: '' }).textContent.replace(/\s+/g, ' ').trim(),
    rowCount: () => document.querySelectorAll('.acct-rows .arow').length,
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

/* ── The page as it opens, mid-sync ─────────────────────────────────── */
await record('boot', async (start) => {
  const c = start('syncing');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyAccountSync,
    liveSig: c.liveSig(),
    navSig: STATES.syncing.nav.account_sync,
    alpha: c.rowState('Alpha'),
    // base.html's own first poll has already run the hook by now, against an unchanged
    // signature - so this is also "nothing changed means nothing is fetched".
    pageFetches: c.pageFetches(),
  };
});

/* ── The sync finishes while the page is open ───────────────────────── */
await record('finished', async (start) => {
  const c = start('syncing');
  await c.settle();
  const before = c.row('Alpha');
  const totalBefore = c.total();
  c.server.state = 'done';
  await c.poll();
  const after = {
    errors: c.errors,
    pageFetches: c.pageFetches(),
    alpha: c.rowState('Alpha'),
    beta: c.rowState('Beta'),
    sameNode: c.row('Alpha') === before,
    liveSig: c.liveSig(),
    navSig: STATES.done.nav.account_sync,
    totalBefore,
    totalAfter: c.total(),
  };
  // Converged: the next poll carries the signature the swapped-in region was rendered at.
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();

  // Handlers are delegated, so the rows that arrived with the swap still navigate - and
  // the row's dead zones still do not.
  const alpha = c.row('Alpha');
  c.click(alpha.querySelector('.a-status'));
  after.navAfterRowClick = c.navigations.length;
  c.click(alpha.querySelector('.spark i[data-tip]'));
  c.click(alpha.querySelector('.a-actions [data-menu]'));
  after.navAfterDeadZoneClicks = c.navigations.length;
  after.menuOpensOnSwappedRow = alpha.querySelector('.menu').classList.contains('open');
  return after;
});

/* ── An open kebab menu holds the refresh back ──────────────────────── */
await record('menu_open', async (start) => {
  const c = start('syncing');
  await c.settle();
  c.click(c.row('Alpha').querySelector('.a-actions [data-menu]'));
  const menuOpen = c.row('Alpha').querySelector('.menu').classList.contains('open');
  c.server.state = 'done';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), alpha: c.rowState('Alpha') };
  c.window.closeMenus();
  await c.poll();
  return {
    errors: c.errors,
    menuOpen,
    whileOpen,
    afterClose: { pageFetches: c.pageFetches(), alpha: c.rowState('Alpha') },
  };
});

/* ── Nothing changed, but the relative times have aged ──────────────── */
await record('aged', async (start) => {
  const c = start('done');
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

/* ── The refresh itself fails ───────────────────────────────────────── */
await record('failed_fetch', async (start) => {
  const c = start('syncing');
  await c.settle();
  c.server.state = 'done';
  c.server.pageStatus = 500;
  await c.poll();
  const failed = {
    pageFetches: c.pageFetches(), alpha: c.rowState('Alpha'),
    liveSig: c.liveSig(), warnings: c.warnings.slice(),
  };
  c.server.pageStatus = 200;
  await c.poll();
  return {
    errors: c.errors,
    failed,
    syncingSig: STATES.syncing.nav.account_sync,
    retried: { pageFetches: c.pageFetches(), alpha: c.rowState('Alpha') },
  };
});

/* ── The first accounts appear on a page that had none ──────────────── */
await record('from_empty', async (start) => {
  // Opened on the empty state; the server has since gained two accounts, and base.html's
  // own first poll is the one that notices.
  const c = start('empty', { serve: 'syncing' });
  const before = { rows: c.rowCount(), empty: Boolean(c.$('.empty-state')) };
  await c.settle();
  return {
    errors: c.errors,
    before,
    after: { rows: c.rowCount(), empty: Boolean(c.$('.empty-state')), total: c.total() },
  };
});

process.stdout.write(JSON.stringify(out), () => process.exit(0));
