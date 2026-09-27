/* Drives the REAL /alerts page in jsdom and prints what it observed as JSON.
   Called by tests/test_alerts_page_js.py, which owns every assertion - this file only
   reports. Same shape as tests/support/accounts_page.mjs.

   What it is for: the page keeps itself current by swapping in a fresh server render when
   base.html's /api/nav-status poll hands it an alert signature other than the one its
   cards were rendered at, and its own actions ask for that same swap instead of editing
   rows (dev/changelog/1131). None of that is reachable from Python.

   Real: every page and nav-status payload is what the Flask app answered at five database
   states (base, raised, read, cleared, empty), base.html's inline poll is what calls the
   hook, and util.js + nav-alerts.js + alerts.js are the shipped files. Faked: the network,
   which answers from whichever state the scenario says the server is in. jsdom implements
   no navigation, so a reload is observed as a "Not implemented: navigation" report.

   argv: <fixture dir> <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATES = {};
for (const s of ['base', 'raised', 'read', 'cleared', 'empty']) {
  STATES[s] = { html: read(`${s}.html`), nav: JSON.parse(read(`${s}.json`)) };
}
const IDS = JSON.parse(read('ids.json'));
const JS = ['util.js', 'nav-alerts.js', 'alerts.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

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

  // `afterPost` is the state a POST moves the server to - what the route really wrote.
  const server = { state: start, pageStatus: 200, afterPost: null };
  const sent = [];
  const posts = [];
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), 'http://localhost:5000/alerts');
    sent.push(url.pathname);
    if ((opts.method || 'GET').toUpperCase() !== 'GET') {
      posts.push(url.pathname);
      if (server.afterPost) server.state = server.afterPost;
    }
    if (url.pathname === '/alerts') {
      const status = server.pageStatus;
      return Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        headers: { get: () => 'text/html' },
        text: () => Promise.resolve(status === 200 ? STATES[server.state].html : 'Internal Server Error'),
        json: () => Promise.reject(new Error('not json')),
      });
    }
    const nav = STATES[server.state].nav;
    const payload = url.pathname === '/api/nav-status' ? nav
      : url.pathname === '/api/alerts/unread_count' ? nav.alerts
        : { success: true };
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
    url: 'http://localhost:5000/alerts',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.matchMedia = (query) => ({
        media: query, matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const row = (key) => $(`#alert-${IDS[key]}`);
  const c = {
    window, document, server, sent, posts, errors, navigations, warnings, $, row,
    settle: () => new Promise((r) => setTimeout(r, 60)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === '/alerts').length,
    liveSig: () => ($('#al-live') || { dataset: {} }).dataset.alertSig,
    unread: () => ($('#al-unread') || { textContent: '' }).textContent.trim(),
    cards: () => ['active', 'past'].filter((k) => $(`#al-card-${k}`)),
    cardRows: (k) => Array.from(document.querySelectorAll(`#alerts-${k} .al-row`))
      .map((r) => Object.keys(IDS).find((key) => String(IDS[key]) === r.dataset.id)),
    isUnread: (key) => (row(key) ? row(key).classList.contains('unread') : null),
    empty: () => Boolean($('#al-live .empty-state')),
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

/* ── The page as it opens ───────────────────────────────────────────── */
await record('boot', async (start) => {
  const c = start('base');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyAlertSignature,
    liveSig: c.liveSig(),
    navSig: STATES.base.nav.alert_signature,
    pageFetches: c.pageFetches(),
  };
});

/* ── A new alert is raised while the page is open ───────────────────── */
await record('raised', async (start) => {
  const c = start('base');
  await c.settle();
  const before = { past: c.cardRows('past'), unread: c.unread(), node: c.row('failed') };
  c.server.state = 'raised';
  await c.poll();
  const after = {
    errors: c.errors,
    before: { past: before.past, unread: before.unread },
    pageFetches: c.pageFetches(),
    past: c.cardRows('past'),
    active: c.cardRows('active'),
    unread: c.unread(),
    sameNode: c.row('failed') === before.node,
    liveSig: c.liveSig(),
    navSig: STATES.raised.nav.alert_signature,
  };
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  return after;
});

/* ── A standing problem clears itself ───────────────────────────────── */
await record('cleared', async (start) => {
  const c = start('read');
  await c.settle();
  const before = c.cards();
  c.server.state = 'cleared';
  await c.poll();
  return { errors: c.errors, before, after: c.cards(), active: c.cardRows('active') };
});

/* ── Everything goes: the empty state arrives without a reload ──────── */
await record('emptied', async (start) => {
  const c = start('cleared');
  await c.settle();
  c.server.state = 'empty';
  await c.poll();
  return {
    errors: c.errors, cards: c.cards(), empty: c.empty(),
    navigations: c.navigations.length, pageFetches: c.pageFetches(),
  };
});

/* ── An open row menu holds a poll-driven refresh back ──────────────── */
await record('menu_open', async (start) => {
  const c = start('base');
  await c.settle();
  c.click(c.row('failed').querySelector('[data-menu]'));
  const menuOpen = c.row('failed').querySelector('.menu').classList.contains('open');
  c.server.state = 'raised';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), past: c.cardRows('past') };
  c.window.closeMenus();
  await c.poll();
  return {
    errors: c.errors, menuOpen, whileOpen,
    afterClose: { pageFetches: c.pageFetches(), past: c.cardRows('past') },
  };
});

/* ── An open "Show details" survives the swap ───────────────────────── */
await record('details_open', async (start) => {
  const c = start('base');
  await c.settle();
  c.click(c.row('failed').querySelector('[data-act="detail"]'));
  const openBefore = !c.row('failed').querySelector('.al-detail').hidden;
  const node = c.row('failed');
  c.server.state = 'raised';
  await c.poll();
  const r = c.row('failed');
  return {
    errors: c.errors,
    openBefore,
    pageFetches: c.pageFetches(),
    sameNode: r === node,
    openAfter: !r.querySelector('.al-detail').hidden,
    toggleLabel: r.querySelector('[data-act="detail"]').textContent.trim(),
  };
});

/* ── Nothing changed, but the ages have aged ────────────────────────── */
await record('aged', async (start) => {
  const c = start('base');
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

/* ── Mark read: the page asks the server, it does not edit the row ──── */
await record('mark_read', async (start) => {
  const c = start('raised');
  await c.settle();
  // The server's page still says unread after the POST: the row must say what the server
  // says, so this shows the page no longer writes its own rows.
  c.click(c.row('failed').querySelector('[data-act="read"]'));
  await c.settle();
  const stale = { posts: c.posts.slice(), pageFetches: c.pageFetches(), unread: c.isUnread('failed') };
  // Now the POST really lands.
  c.server.afterPost = 'read';
  c.click(c.row('failed').querySelector('[data-act="read"]'));
  await c.settle();
  return {
    errors: c.errors, stale,
    landed: {
      pageFetches: c.pageFetches(), unread: c.isUnread('failed'),
      unreadLabel: c.unread(), liveSig: c.liveSig(), navSig: STATES.read.nav.alert_signature,
      navCountFetched: c.sent.includes('/api/alerts/unread_count'),
      readItem: Boolean(c.row('failed').querySelector('[data-act="read"]')),
    },
  };
});

/* ── The refresh itself fails ───────────────────────────────────────── */
await record('failed_fetch', async (start) => {
  const c = start('base');
  await c.settle();
  c.server.state = 'raised';
  c.server.pageStatus = 500;
  await c.poll();
  const failed = { pageFetches: c.pageFetches(), past: c.cardRows('past'), warnings: c.warnings.slice() };
  c.server.pageStatus = 200;
  await c.poll();
  return { errors: c.errors, failed, retried: { pageFetches: c.pageFetches(), past: c.cardRows('past') } };
});

process.stdout.write(JSON.stringify(out), () => process.exit(0));
