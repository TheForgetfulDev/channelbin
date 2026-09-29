/* Drives the REAL /jobs page in jsdom and prints what it observed as JSON.
   Called by tests/test_jobs_page_js.py, which owns every assertion - this file only
   reports. Same shape as tests/support/accounts_page.mjs.

   What it is for: the jobs table keeps itself current by swapping in a fresh server render
   when base.html's /api/nav-status poll reports that the running background work or the
   recording signature moved, and once a minute in a visible tab (dev/changelog/1164). None
   of that is reachable from Python - a page whose hooks never registered renders exactly
   the same markup.

   Real: every page and the nav-status payload are what the Flask app answered, base.html's
   own inline poll is what calls the hooks, and util.js + jobs.js are the shipped files.
   Faked: the network, and the nav-status payload's background tasks and recording
   signature, which a scenario rewrites to say "something changed".

   argv: <fixture dir> <repo root>. The fixture dir holds empty.html, a.html, b.html and
   nav.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGES = { empty: read('empty.html'), a: read('a.html'), b: read('b.html') };
const NAV = JSON.parse(read('nav.json'));
const JS = ['util.js', 'jobs.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot(start, { serve = start } = {}) {
  const errors = [];
  const navigations = [];
  const warnings = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation|reload/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  vc.on('warn', (...a) => warnings.push(a.map(String).join(' ')));

  // What the "server" currently answers. A scenario moves it.
  const server = { page: serve, nav: JSON.parse(JSON.stringify(NAV)) };
  const sent = [];
  const posts = [];
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), 'http://localhost:5000/jobs');
    sent.push(url.pathname);
    if ((opts.method || 'GET').toUpperCase() !== 'GET') posts.push(url.pathname);
    if (url.pathname === '/jobs') {
      const body = PAGES[server.page];
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => 'text/html' },
        text: () => Promise.resolve(body),
        json: () => Promise.reject(new Error('not json')),
      });
    }
    const payload = url.pathname === '/api/nav-status' ? server.nav : { success: true };
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(PAGES[start], {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/jobs',
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
  const c = {
    window, document, server, sent, posts, errors, navigations, warnings, $,
    settle: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === '/jobs').length,
    rows: () => Array.from(document.querySelectorAll('#jb-live tbody tr'))
      .map((tr) => tr.querySelector('.jb-name').textContent.trim()),
    count: () => ($('#jb-count') || { textContent: '' }).textContent.trim(),
    // The two things base.html's poll reports that mean "the job list moved".
    syncStarts: () => {
      server.nav.activity.background.tasks = [{ kind: 'account_sync', label: 'Syncing Alpha' }];
    },
    recordingMoves: () => { server.nav.recording_signature = `${server.nav.recording_signature}-moved`; },
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
  const c = start('a');
  await c.settle();
  await c.poll();
  return {
    errors: c.errors,
    hooks: [typeof c.window.__applyBackgroundTasks, typeof c.window.__applyRecordingSignature],
    refreshButton: Boolean(c.$('#jb-refresh')),
    // base.html's own first poll and one more have run, with nothing changed.
    pageFetches: c.pageFetches(),
    rows: c.rows(),
  };
});

/* ── A sync starts while the page is open ───────────────────────────── */
await record('background_change', async (start) => {
  const c = start('a');
  await c.settle();
  c.server.page = 'b';
  c.syncStarts();
  await c.poll();
  const after = { pageFetches: c.pageFetches(), rows: c.rows(), count: c.count() };
  await c.poll();
  return { errors: c.errors, after, afterNextPoll: c.pageFetches() };
});

/* ── A recording is scheduled or starts elsewhere ───────────────────── */
await record('recording_change', async (start) => {
  const c = start('a');
  await c.settle();
  c.server.page = 'b';
  c.recordingMoves();
  await c.poll();
  return { errors: c.errors, pageFetches: c.pageFetches(), rows: c.rows() };
});

/* ── An open kebab menu holds the refresh back ──────────────────────── */
await record('menu_open', async (start) => {
  const c = start('a');
  await c.settle();
  c.click(c.$('#jb-live [data-menu]'));
  const menuOpen = Boolean(c.$('#jb-live .menu.open'));
  c.server.page = 'b';
  c.syncStarts();
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), rows: c.rows() };
  c.window.closeMenus();
  // Nothing new is reported on this poll: the change was seen while the menu was open.
  await c.poll();
  return { errors: c.errors, menuOpen, whileOpen, afterClose: { pageFetches: c.pageFetches(), rows: c.rows() } };
});

/* ── The render ages with nothing changing ──────────────────────────── */
await record('aged', async (start) => {
  const c = start('a');
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

/* ── Skip next run, through its modal ───────────────────────────────── */
await record('skip_action', async (start) => {
  const c = start('a');
  await c.settle();
  c.click(c.$('#jb-live [data-menu]'));
  c.click(c.$('#jb-live .menu-item[data-act="skip"]'));
  const modal = c.$('.modal');
  const skipIt = modal && Array.from(modal.querySelectorAll('button'))
    .find((b) => b.textContent.trim() === 'Skip it');
  c.server.page = 'b';
  if (skipIt) c.click(skipIt);
  // Longer than the 1.5s the page used to wait before reloading itself.
  await c.settle(1700);
  return {
    errors: c.errors,
    modalShown: Boolean(skipIt),
    posts: c.posts,
    pageFetches: c.pageFetches(),
    rows: c.rows(),
    navigations: c.navigations.length,
    modalGone: !c.$('.modal'),
  };
});

/* ── The first jobs appear on a page that had none ──────────────────── */
await record('from_empty', async (start) => {
  const c = start('empty', { serve: 'a' });
  await c.settle();
  const before = { rows: c.rows().length, empty: Boolean(c.$('#jb-live .empty-state')), count: c.count() };
  c.syncStarts();
  await c.poll();
  return {
    errors: c.errors,
    before,
    after: { rows: c.rows().length, empty: Boolean(c.$('#jb-live .empty-state')), count: c.count() },
  };
});

process.stdout.write(JSON.stringify(out), () => process.exit(0));
