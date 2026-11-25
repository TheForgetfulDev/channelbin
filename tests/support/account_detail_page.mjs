/* Drives the REAL /accounts/<id> page in jsdom and prints what it observed as JSON.
   Called by tests/test_account_detail_page_js.py, which owns every assertion - this file
   only reports, so a failure reads as "the page did X" in Python rather than as a node exit
   code. Same shape as tests/support/accounts_page.mjs.

   What it is for: the account page keeps itself current through a sync by swapping its
   live regions for a fresh server render when base.html's /api/nav-status poll hands it a
   sync signature it was not rendered at, or the render is a minute old
   (dev/changelog/1152). None of that is reachable from Python - a page whose hook never
   registered renders exactly the same markup.

   What is real here and what is not: every page, nav-status payload and syncs-API answer
   is what the Flask app really answered at three database states (never synced; a sync
   running; that sync finished), base.html's own inline poll is what calls the hook, and
   util.js + the four account scripts are the shipped files evaluated the way their
   <script src> would have been. Faked: the network, which answers from whichever state the
   scenario says the server is in, and IntersectionObserver, which jsdom lacks - the stub
   only records what it was asked to watch.

   argv: <fixture dir> <repo root>. The fixture dir holds meta.json and, for each state,
   <state>.fresh.html, <state>.busy.html, <state>.nav.json and <state>.syncs.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const META = JSON.parse(read('meta.json'));
const STATES = {};
for (const s of ['never', 'syncing', 'done']) {
  STATES[s] = {
    fresh: read(`${s}.fresh.html`),
    busy: read(`${s}.busy.html`),
    nav: JSON.parse(read(`${s}.nav.json`)),
    syncs: JSON.parse(read(`${s}.syncs.json`)),
    scores: JSON.parse(read(`${s}.scores.json`)),
    ordered: read(`${s}.ordered.html`),
    preview: JSON.parse(read(`${s}.preview.json`)),
    settings: JSON.parse(read(`${s}.settings.json`)),
  };
}
const JS = ['util.js', 'account-modal.js', 'account-actions.js', 'account-detail.js', 'account-stats.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot(which, start, { serve = start, scoresStatus = 200 } = {}) {
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

  const id = META[which];
  const pagePath = `/accounts/${id}`;
  const server = { state: serve, pageStatus: 200, postStatus: 200, scoresStatus, preview: null };
  const sent = [];
  const previews = [];
  const posts = [];
  const respond = (status, payload, text) => Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => (text === undefined ? 'application/json' : 'text/html') },
    json: () => (text === undefined ? Promise.resolve(payload) : Promise.reject(new Error('not json'))),
    text: () => Promise.resolve(text === undefined ? JSON.stringify(payload) : text),
  });
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), `http://localhost:5000${pagePath}`);
    sent.push(url.pathname);
    const st = STATES[server.state];
    if ((opts.method || 'GET').toUpperCase() !== 'GET') {
      posts.push({ path: url.pathname, body: opts.body });
      if (server.postStatus !== 200) return respond(server.postStatus, { error: 'Refused.' });
      return respond(200, { success: true, message: 'Done.' });
    }
    if (url.pathname === pagePath) {
      const status = server.pageStatus;
      return respond(status, null, status === 200 ? st[which] : 'Internal Server Error');
    }
    if (url.pathname.endsWith('-preview')) {
      previews.push(url.pathname + url.search);
      if (server.previewStatus) return respond(server.previewStatus, { error: 'Preview broke.' });
      return respond(200, server.preview || st.preview);
    }
    if (url.pathname === `/api/accounts/${id}/epg-sources/${META.second}`) return respond(200, st.settings);
    if (url.pathname === '/api/nav-status') return respond(200, st.nav);
    if (url.pathname === `/api/accounts/${id}/syncs`) return respond(200, st.syncs);
    if (url.pathname === `/api/accounts/${id}/epg-scorecard`) {
      if (server.scoresStatus !== 200) return respond(server.scoresStatus, { error: 'Failed.' });
      return respond(200, st.scores);
    }
    return respond(200, { success: true });
  };

  const observers = [];
  const dom = new JSDOM(STATES[start][which], {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `http://localhost:5000${pagePath}`,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.matchMedia = (query) => ({
        media: query, matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
      w.IntersectionObserver = class {
        constructor(cb) { this.cb = cb; this.targets = []; observers.push(this); }
        observe(el) { this.targets.push(el); }
        disconnect() { this.targets = []; }
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));

  const $ = (s) => document.querySelector(s);
  const text = (s) => ($(s) ? $(s).textContent.replace(/\s+/g, ' ').trim() : null);
  const c = {
    window, document, server, sent, posts, previews, errors, navigations, warnings, observers, $,
    settle: () => new Promise((r) => setTimeout(r, 60)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === pagePath).length,
    syncsFetches: () => sent.filter((p) => p === `/api/accounts/${id}/syncs`).length,
    scoreFetches: () => sent.filter((p) => p === `/api/accounts/${id}/epg-scorecard`).length,
    snap: () => ({
      barMsg: text('#acct-actionbar .gd-ab-msg'),
      barActs: Array.from(document.querySelectorAll('#acct-actionbar .gd-ab-actions > [data-act]'))
        .map((b) => b.dataset.act),
      stickyActs: Array.from(document.querySelectorAll('#acct-stickybar [data-act]'))
        .map((b) => b.dataset.act),
      headBadge: text('#acct-head .gd-title .badge'),
      stateStatus: window.ACCOUNT_DETAIL.status,
      stateSig: window.ACCOUNT_DETAIL.syncSig,
      histRows: document.querySelectorAll('#acct-hist .hist-row').length,
      histBadges: Array.from(document.querySelectorAll('#acct-hist .hist-row .h-res'))
        .map((b) => b.textContent.trim()),
      histDurs: Array.from(document.querySelectorAll('#acct-hist .hist-row .h-dur .mono'))
        .map((b) => b.textContent.trim()),
      histEmpty: Boolean($('#acct-hist-body .empty-state')),
      moreBtn: text('#acct-hist-more'),
      activity: text('#acct-activity-body'),
      scoreLines: Array.from(document.querySelectorAll('[data-score-for]'))
        .map((el) => ({ hidden: el.hidden, text: el.textContent.trim(),
          tip: el.querySelector('[data-tip]') ? el.querySelector('[data-tip]').getAttribute('data-tip') : null })),
      reachTip: $('#acct-reach-key') ? $('#acct-reach-key').getAttribute('data-tip') : null,
    }),
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
  const c = start('busy', 'syncing');
  await c.settle();
  return {
    errors: c.errors,
    warnings: c.warnings,
    hook: typeof c.window.__applyAccountSync,
    navSig: STATES.syncing.nav.account_sync,
    pageFetches: c.pageFetches(),
    ...c.snap(),
  };
});

/* ── The sync finishes while the page is open ───────────────────────── */
await record('finished', async (start) => {
  const c = start('busy', 'syncing');
  await c.settle();
  const before = c.snap();
  const sticky = c.$('#acct-stickybar');
  const oldBar = c.$('#acct-actionbar');
  const histBox = c.$('#acct-hist');
  c.server.state = 'done';
  await c.poll();
  const io = c.observers[0];
  const after = {
    errors: c.errors,
    before,
    after: c.snap(),
    pageFetches: c.pageFetches(),
    navSig: STATES.done.nav.account_sync,
    sameStickyBar: c.$('#acct-stickybar') === sticky,
    barReplaced: c.$('#acct-actionbar') !== oldBar,
    histBoxKept: c.$('#acct-hist') === histBox,
    observed: io ? io.targets.map((t) => (t === c.$('#acct-actionbar') ? 'current' : 'stale')) : null,
  };
  // Converged: the next poll carries the signature the page was rendered at.
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  // The phone's action sheet reads the status from the refreshed state, not the load-time one.
  c.click(c.$('#acct-stickybar [data-act="page-actions"]'));
  const force = c.$('.acct-sheet [data-act="force-epg"]');
  after.sheetForceEpgDisabled = force ? force.disabled : null;
  return after;
});

/* ── The first sync, on a page opened before it ─────────────────────── */
await record('first_sync', async (start) => {
  const c = start('fresh', 'never');
  await c.settle();
  const before = c.snap();
  c.server.state = 'syncing';
  await c.poll();
  const during = c.snap();
  c.server.state = 'done';
  await c.poll();
  return { errors: c.errors, warnings: c.warnings, before, during, after: c.snap() };
});

/* ── An open kebab menu holds the refresh back ──────────────────────── */
await record('menu_open', async (start) => {
  const c = start('busy', 'syncing');
  await c.settle();
  c.click(c.$('#acct-actionbar [data-menu]'));
  const menuOpen = c.$('#acct-kebab').classList.contains('open');
  c.server.state = 'done';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), ...c.snap() };
  c.window.closeMenus();
  await c.poll();
  return {
    errors: c.errors, menuOpen, whileOpen,
    afterClose: { pageFetches: c.pageFetches(), ...c.snap() },
  };
});

/* ── Nothing changed, but the relative times have aged ──────────────── */
await record('aged', async (start) => {
  const c = start('busy', 'done');
  await c.settle();
  const fresh = c.pageFetches();
  const histBox = c.$('#acct-hist');
  const firstRow = c.$('#acct-hist .hist-row');
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => true });
  await c.poll();
  const hiddenTab = c.pageFetches();
  Object.defineProperty(c.document, 'hidden', { configurable: true, get: () => false });
  await c.poll();
  const visibleTab = c.pageFetches();
  await c.poll();
  return {
    errors: c.errors, fresh, hiddenTab, visibleTab, rightAfter: c.pageFetches(),
    // Unchanged runs are not redrawn, so a row the pointer is on is left alone.
    sameHistRow: c.$('#acct-hist') === histBox && c.$('#acct-hist .hist-row') === firstRow,
  };
});

/* ── The refresh itself fails ───────────────────────────────────────── */
await record('failed_fetch', async (start) => {
  const c = start('busy', 'syncing');
  await c.settle();
  c.server.state = 'done';
  c.server.pageStatus = 500;
  await c.poll();
  const failed = { pageFetches: c.pageFetches(), warnings: c.warnings.slice(), ...c.snap() };
  c.server.pageStatus = 200;
  await c.poll();
  return {
    errors: c.errors,
    failed,
    syncingSig: STATES.syncing.nav.account_sync,
    retried: { pageFetches: c.pageFetches(), ...c.snap() },
  };
});

/* ── "All N syncs" is open when the sync finishes ───────────────────── */
await record('expanded', async (start) => {
  const c = start('busy', 'syncing');
  await c.settle();
  const collapsed = c.snap();
  c.click(c.$('#acct-hist-more'));
  await c.settle();
  const opened = { syncsFetches: c.syncsFetches(), ...c.snap() };
  c.server.state = 'done';
  await c.poll();
  const refreshed = { syncsFetches: c.syncsFetches(), ...c.snap() };
  // The button arrived with the swap; its handler is delegated, so it still collapses.
  c.click(c.$('#acct-hist-more'));
  await c.settle();
  return { errors: c.errors, collapsed, opened, refreshed, closed: c.snap() };
});

/* ── An action refreshes the page in place rather than reloading it ─── */
await record('action', async (start) => {
  const c = start('busy', 'syncing');
  await c.settle();
  c.server.state = 'done';
  c.click(c.$('#acct-actionbar [data-act="cancel-sync"]'));
  await c.settle();
  return {
    errors: c.errors,
    posts: c.posts.map((p) => p.path),
    pageFetches: c.pageFetches(),
    navigations: c.navigations.length,
    ...c.snap(),
  };
});

/* ── A Use switch posts its value, refreshes in place, and goes back if refused ─── */
await record('switch', async (start) => {
  const c = start('busy', 'done');
  await c.settle();
  const flip = (on) => {
    const input = c.$('input[data-switch="host"]');
    input.checked = on;
    input.dispatchEvent(new c.window.Event('change', { bubbles: true }));
  };
  flip(false);
  await c.settle();
  const after = { posts: c.posts.slice(), pageFetches: c.pageFetches() };
  c.server.postStatus = 409;
  flip(false);
  await c.settle();
  const input = c.$('input[data-switch="host"]');
  return {
    errors: c.errors.filter((e) => !/Refused/.test(e)),
    hostId: META.host,
    after,
    refusedChecked: input.checked,
    refusedDisabled: input.disabled,
    refusedPageFetches: c.pageFetches(),
  };
});

/* ── The scorecard line loads after the page and survives the live swap ── */
await record('scores', async (start) => {
  const c = start('busy', 'done');
  const atFirstPaint = c.snap();
  await c.settle();
  const loaded = { scoreFetches: c.scoreFetches(), ...c.snap() };
  // A minute later nothing has synced: the regions are swapped, the line is put back from
  // what was read, and the API is not asked again.
  const realNow = c.window.Date.now.bind(c.window.Date);
  c.window.Date.now = () => realNow() + 61 * 1000;
  await c.poll();
  const aged = { pageFetches: c.pageFetches(), scoreFetches: c.scoreFetches(), ...c.snap() };
  c.window.Date.now = realNow;
  // A sync runs: the swap carries a new signature, so the line is read again.
  c.server.state = 'syncing';
  await c.poll();
  const synced = { pageFetches: c.pageFetches(), scoreFetches: c.scoreFetches(), ...c.snap() };
  return { errors: c.errors, atFirstPaint, loaded, aged, synced };
});

/* ── The scorecard read fails: the page stays as it was drawn ─────────── */
await record('scores_failed', async (start) => {
  const c = start('busy', 'done', { scoresStatus: 500 });
  await c.settle();
  return { errors: c.errors, warnings: c.warnings, ...c.snap() };
});

/* ── Move up asks the server what it would change, and moves only on the OK ── */
await record('move_preview', async (start) => {
  const c = start('ordered', 'done');
  await c.settle();
  const dialog = () => {
    const panels = c.document.querySelectorAll('.modal .modal-panel');
    const p = panels[panels.length - 1];
    if (!p) return null;
    return {
      title: p.querySelector('.modal-head') ? p.querySelector('.modal-head').textContent.trim() : null,
      items: Array.from(p.querySelectorAll('.confirm-list li')).map((li) => li.textContent.trim()),
      paras: Array.from(p.querySelectorAll('.modal-body p')).map((x) => x.textContent.trim()),
      buttons: Array.from(p.querySelectorAll('.modal-foot .btn')),
    };
  };
  const moveUp = () => c.click(c.$(`[data-act="source-move"][data-direction="up"][data-source-id="${META.second}"]`));
  moveUp();
  await c.settle();
  const shown = dialog();
  const asked = { previews: c.previews.slice(), posts: c.posts.length, title: shown && shown.title,
    items: shown && shown.items, paras: shown && shown.paras };
  shown.buttons[0].click();
  await c.settle();
  const cancelled = { posts: c.posts.length, open: Boolean(dialog()) };
  c.server.preview = { ...STATES.done.preview, own_order: 'This gives Ordered its own EPG order.' };
  moveUp();
  await c.settle();
  const own = dialog();
  const ownParas = own.paras;
  own.buttons[own.buttons.length - 1].click();
  await c.settle();
  const ownPosts = c.posts.map((x) => ({ path: x.path, body: JSON.parse(x.body || '{}') }));
  // The preview itself fails: the dialog still opens, says so, and the move can go ahead.
  c.server.preview = null;
  c.server.previewStatus = 500;
  moveUp();
  await c.settle();
  const broken = dialog();
  const brokenItems = broken ? broken.items : null;
  if (broken) broken.buttons[broken.buttons.length - 1].click();
  await c.settle();
  return {
    errors: c.errors.filter((e) => !/Preview broke/.test(e)), asked, cancelled, ownParas,
    served: STATES.done.preview.lines, posts: ownPosts, second: META.second, brokenItems,
    brokenPosts: c.posts.slice(ownPosts.length)
      .map((x) => ({ path: x.path, body: JSON.parse(x.body || '{}') })),
  };
});

// The Edit source dialog shows the expiration date the server holds and sends back what the
// field says, a cleared field as '' (dev/changelog/1724).
await record('source_expiry', async (start) => {
  const c = start('ordered', 'done');
  await c.settle();
  const field = () => c.document.querySelector('.modal-panel #src-expires');
  const save = () => {
    const btns = c.document.querySelectorAll('.modal-panel .modal-foot .btn');
    btns[btns.length - 1].click();
  };
  const edit = () => c.click(c.$(`[data-act="source-edit"][data-source-id="${META.second}"]`));
  edit();
  await c.settle();
  const shown = field() ? { type: field().type, value: field().value } : null;
  field().value = '2027-02-01';
  save();
  await c.settle();
  edit();
  await c.settle();
  field().value = '';
  save();
  await c.settle();
  return {
    errors: c.errors, shown,
    bodies: c.posts.map((x) => ({ path: x.path, body: JSON.parse(x.body || '{}') })),
    second: META.second,
  };
});

process.stdout.write(JSON.stringify(out), () => process.exit(0));
