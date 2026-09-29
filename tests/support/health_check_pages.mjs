/* Drives the REAL /channel-groups and /channels/<id> pages in jsdom and prints what it
   observed as JSON. Called by tests/test_health_check_pages_js.py, which owns every
   assertion - this file only reports. Same shape as tests/support/accounts_page.mjs.

   What it is for: both pages follow a health check run by swapping in a fresh server render
   when base.html's /api/nav-status poll hands over a health-check signature that no longer
   matches the one they were rendered at (dev/changelog/1158). None of that is reachable
   from Python - a page whose hook never registered serves exactly the same markup.

   Real: every page and nav-status payload is what the Flask app answered at three database
   states (idle, a run testing the channel, that run finished), base.html's own inline poll
   is what calls the hook, and the scripts are the shipped files. Faked: the network, which
   answers from whichever state the scenario says the server is in. guide.js is not loaded -
   the What's on card is its region, and the test is that the swap leaves it alone.

   argv: <fixture dir> <repo root>. The fixture dir holds meta.json and, per state in
   idle/running/done, groups-<state>.html, channel-<state>.html and nav-<state>.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const META = JSON.parse(read('meta.json'));
const PAGES = {
  groups: {
    path: '/channel-groups',
    js: ['util.js', 'format-plan.js', 'create-group-modal.js', 'clone-modal.js',
      'group-delete.js', 'filter-bar.js', 'groups.js'],
  },
  channel: {
    path: META.channelPath,
    js: ['util.js', 'schedule-fields.js', 'check-modal.js', 'missing-modal.js',
      'channel-detail.js'],
  },
};
const STATES = {};
for (const s of ['idle', 'running', 'done']) {
  STATES[s] = {
    groups: read(`groups-${s}.html`), channel: read(`channel-${s}.html`),
    nav: JSON.parse(read(`nav-${s}.json`)),
  };
}

function boot(page, start, { query = '' } = {}) {
  const spec = PAGES[page];
  const errors = [];
  const navigations = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const server = { state: start };
  const sent = [];
  const posts = [];
  const fetchStub = (target, opts = {}) => {
    const url = new URL(String(target), `http://localhost:5000${spec.path}`);
    sent.push(url.pathname);
    if ((opts.method || 'GET').toUpperCase() !== 'GET') posts.push(url.pathname);
    if (url.pathname === spec.path) {
      const body = STATES[server.state][page];
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => 'text/html' },
        text: () => Promise.resolve(body), json: () => Promise.reject(new Error('not json')),
      });
    }
    const payload = url.pathname === '/api/nav-status' ? STATES[server.state].nav
      : url.pathname.endsWith('/screenshot')
        ? { success: true, source: 'capture', url: '/channel-tests/screenshots/manual-ch1.jpg?v=1' }
        : { success: true, message: 'Testing now.' };
    return Promise.resolve({
      ok: true, status: 200, headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload), text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(STATES[start][page], {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `http://localhost:5000${spec.path}${query}`,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.matchMedia = (q) => ({
        media: q, matches: false, onchange: null, addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  spec.js.forEach((f) => window.eval(fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8')));

  const c = {
    window, document, server, sent, posts, errors, navigations,
    $: (s) => document.querySelector(s),
    $$: (s) => Array.from(document.querySelectorAll(s)),
    settle: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    pageFetches: () => sent.filter((p) => p === spec.path).length,
    text: (s) => (document.querySelector(s) || { textContent: '' }).textContent.replace(/\s+/g, ' ').trim(),
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

/* ── Groups list ─────────────────────────────────────────────────────── */
const item = (c, name) => c.$(`.grp-item[data-name="${name}"]`);
const memberOrder = (c, name) => Array.from(item(c, name).querySelectorAll('tr.grp-mrow'))
  .map((r) => r.dataset.name);

await record('groups_boot', async (start) => {
  const c = start('groups', 'running');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyHealthCheck,
    listSig: c.$('#grp-list-groups').dataset.hcSig,
    navSig: STATES.running.nav.health_check.sig,
    running: c.$$('#grp-list-groups .badge.b-running').length,
    pageFetches: c.pageFetches(),
  };
});

await record('groups_finished', async (start) => {
  // A search and a filter chip that each hide "other", a non-default section sort, one
  // group open, and its member table sorted by name descending - all state a swap drops.
  const c = start('groups', 'running', { query: '?q=feed&f=guide:in&sort=groups:members:-1' });
  await c.settle();
  const pair = item(c, 'pair');
  const other = item(c, 'other');
  c.click(pair.querySelector('.grp-expand'));
  const nameTh = pair.querySelector('th[data-msort="name"]');
  c.click(nameTh);
  c.click(nameTh);
  const before = {
    order: memberOrder(c, 'pair'),
    running: c.$$('#grp-list-groups .badge.b-running').length,
    count: c.text('#filter-count'),
    sortChip: c.text('#sort-chip-groups'),
  };
  c.server.state = 'done';
  await c.poll();
  const pairAfter = item(c, 'pair');
  const after = {
    errors: c.errors,
    before,
    pageFetches: c.pageFetches(),
    swapped: pairAfter !== pair && item(c, 'other') !== other,
    listSig: c.$('#grp-list-groups').dataset.hcSig,
    navSig: STATES.done.nav.health_check.sig,
    running: c.$$('#grp-list-groups .badge.b-running').length,
    statuses: Array.from(pairAfter.querySelectorAll('tr.grp-mrow')).map((r) => r.dataset.status),
    expanded: !pairAfter.querySelector('.grp-detail').hidden
      && pairAfter.querySelector('.grp-expand').getAttribute('aria-expanded') === 'true',
    order: memberOrder(c, 'pair'),
    nameThSorted: pairAfter.querySelector('th[data-msort="name"]').classList.contains('sorted'),
    otherHidden: item(c, 'other').style.display === 'none',
    pairShown: pairAfter.style.display !== 'none',
    count: c.text('#filter-count'),
    chips: c.$$('#filter-chips .chip.active-filter').length,
    search: c.$('#grp-search').value,
    sortChip: c.text('#sort-chip-groups'),
  };
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  // Handlers are delegated, so the swapped-in rows still expand and collapse.
  c.click(pairAfter.querySelector('.grp-expand'));
  after.collapsesAfterSwap = pairAfter.querySelector('.grp-detail').hidden;
  return after;
});

await record('groups_menu_open', async (start) => {
  const c = start('groups', 'running');
  await c.settle();
  c.click(item(c, 'pair').querySelector('.grp-actions [data-menu]'));
  const menuOpen = Boolean(c.$('.menu.open'));
  c.server.state = 'done';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), running: c.$$('.badge.b-running').length };
  c.window.closeMenus();
  await c.poll();
  return {
    errors: c.errors, menuOpen, whileOpen,
    afterClose: { pageFetches: c.pageFetches(), running: c.$$('.badge.b-running').length },
  };
});

/* ── Channel page ────────────────────────────────────────────────────── */
const state = (c) => JSON.parse(c.$('#cd-health-state').textContent);

await record('channel_boot', async (start) => {
  const c = start('channel', 'running');
  await c.settle();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyHealthCheck,
    barSig: c.$('#cd-statusbar').dataset.hcSig,
    navSig: STATES.running.nav.health_check.sig,
    bar: c.text('#cd-statusbar .badge'),
    testerBusy: c.window.CHANNEL_DETAIL.testerBusy,
    pageFetches: c.pageFetches(),
  };
});

await record('channel_finished', async (start) => {
  const c = start('channel', 'running');
  await c.settle();
  const whatson = c.$('[data-section="whatson"]');
  const tests = c.$('#cd-tests');
  const bd = c.$('#cd-timeline details[data-bd]');
  bd.open = true;
  const sticky = c.$('.cd-stickybar [data-act="test-now"]');
  sticky.disabled = true;
  const before = {
    bar: c.text('#cd-statusbar .badge'),
    rows: c.$$('#cd-tests tbody tr').length,
    available: state(c).healthRollback.available,
    openKey: bd.dataset.bd,
  };
  c.server.state = 'done';
  await c.poll();
  const after = {
    errors: c.errors,
    before,
    pageFetches: c.pageFetches(),
    swapped: c.$('#cd-tests') !== tests,
    barSig: c.$('#cd-statusbar').dataset.hcSig,
    navSig: STATES.done.nav.health_check.sig,
    bar: c.text('#cd-statusbar .badge'),
    rows: c.$$('#cd-tests tbody tr').length,
    inProgress: c.$$('#cd-tests .badge-in_progress').length,
    whatsonKept: c.$('[data-section="whatson"]') === whatson,
    openKeys: c.$$('#cd-timeline details[data-bd][open]').map((d) => d.dataset.bd),
    testerBusy: c.window.CHANNEL_DETAIL.testerBusy,
    available: state(c).healthRollback.available,
    stickyEnabled: !sticky.disabled,
  };
  await c.poll();
  after.pageFetchesAfterNextPoll = c.pageFetches();
  // The step-back confirm quotes the swapped state, not the one the page loaded with.
  c.click(c.$('#cd-health [data-act="health-step-back"]'));
  after.stepBackBody = c.text('.modal .modal-body');
  return after;
});

await record('channel_test_now', async (start) => {
  const c = start('channel', 'idle');
  await c.settle();
  c.click(c.$('#cd-statusbar [data-act="test-now"]'));
  // Past the retired reload poll's first 1.5s tick.
  await c.settle(2000);
  return {
    errors: c.errors,
    posts: c.posts,
    statusPolls: c.sent.filter((p) => p === '/api/channel-tests/status').length,
    navigations: c.navigations.length,
    disabled: c.$('#cd-statusbar [data-act="test-now"]').disabled,
  };
});

await record('channel_screenshot', async (start) => {
  const c = start('channel', 'idle');
  await c.settle();
  const inKebab = Boolean(c.$('#cd-kebab [data-act="screenshot"]'));
  c.click(c.$('#cd-statusbar [data-act="screenshot"]'));
  const disabledWhileRunning = c.$('#cd-statusbar [data-act="screenshot"]').disabled;
  await c.settle(200);
  const lb = c.$('#app-lightbox');
  return {
    errors: c.errors,
    inKebab,
    disabledWhileRunning,
    posts: c.posts,
    pageFetches: c.pageFetches(),
    lightboxShown: Boolean(lb && lb.classList.contains('show')),
    lightboxSrc: lb ? lb.querySelector('img').getAttribute('src') : null,
    enabledAfter: c.$$('[data-act="screenshot"]').every((b) => !b.disabled),
    navigations: c.navigations.length,
  };
});

await record('channel_menu_open', async (start) => {
  const c = start('channel', 'running');
  await c.settle();
  c.click(c.$('#cd-kebab').previousElementSibling);
  const menuOpen = c.$('#cd-kebab').classList.contains('open');
  c.server.state = 'done';
  await c.poll();
  const whileOpen = { pageFetches: c.pageFetches(), bar: c.text('#cd-statusbar .badge') };
  c.window.closeMenus();
  await c.poll();
  return {
    errors: c.errors, menuOpen, whileOpen,
    afterClose: { pageFetches: c.pageFetches(), bar: c.text('#cd-statusbar .badge') },
  };
});

process.stdout.write(JSON.stringify(out), () => process.exit(0));
