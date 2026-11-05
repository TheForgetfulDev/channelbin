/* Drives the REAL Accounts list's Sync All button in jsdom and prints what it observed as JSON.
   Called by tests/test_accounts_sync_all_js.py, which owns every assertion - this file only
   reports.

   What is real: the markup /accounts rendered and the status read the app answered, at each
   state, and util.js + sync-queue.js + accounts.js as shipped. Faked: the network, which answers
   from whichever state the scenario puts the server in and records every request.

   argv: <fixture dir> <repo root>. The fixture dir holds <state>.html and <state>.json for each
   of two, one, held and running. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATES = {};
for (const s of ['two', 'one', 'held', 'running']) {
  STATES[s] = { html: read(`${s}.html`), info: JSON.parse(read(`${s}.json`)) };
}
const JS = ['util.js', 'account-modal.js', 'account-actions.js', 'sync-queue.js', 'accounts.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const QUEUE = '/api/accounts/sync-queue';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function boot(start) {
  const errors = [];
  const requests = [];
  const server = { state: start, afterPost: null };
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const respond = (body) => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
  const dom = new JSDOM(STATES[start].html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/accounts',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (target, init = {}) => {
        const path = new URL(String(target), 'http://localhost:5000/accounts').pathname;
        const method = (init.method || 'GET').toUpperCase();
        requests.push({ path, method, body: init.body || null });
        if (path === '/accounts') {
          const { html } = STATES[server.state];
          return Promise.resolve({
            ok: true, status: 200, headers: { get: () => 'text/html' },
            text: () => Promise.resolve(html),
          });
        }
        if (path === QUEUE && method === 'GET') return respond(STATES[server.state].info);
        if (method === 'POST' && server.afterPost) server.state = server.afterPost;
        return respond({ success: true, message: 'Server message.' });
      };
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  return {
    errors,
    requests,
    server,
    window,
    clickSyncAll: () => document.querySelector('[data-sync-all]')
      .dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    label: () => {
      const b = document.querySelector('#acct-sync-all button');
      return b ? b.textContent.trim() : null;
    },
    modalText: () => {
      const panels = $$('.modal-panel');
      return panels.length ? panels[panels.length - 1].textContent.replace(/\s+/g, ' ').trim() : '';
    },
    modalButtons: () => $$('.modal-panel button').map((b) => b.textContent.trim()).filter(Boolean),
    press: (label) => $$('.modal-panel button').find((b) => b.textContent.trim() === label)
      .dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    toasts: () => $$('.toast').map((t) => t.textContent.replace(/\s+/g, ' ').trim()),
    posts: () => requests.filter((r) => r.method === 'POST'),
    pageReads: () => requests.filter((r) => r.path === '/accounts').length,
  };
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

const openDialog = async (c) => {
  await wait(20);
  c.clickSyncAll();
  await wait(30);
};

await record('confirmed', async () => {
  const c = boot('two');
  await openDialog(c);
  const before = { text: c.modalText(), buttons: c.modalButtons(), posts: c.posts().length };
  c.server.afterPost = 'running';
  c.press('Sync 2 accounts');
  await wait(50);
  return { errors: c.errors, before, posts: c.posts(), toasts: c.toasts(), label: c.label(),
    modalGone: c.modalButtons().length === 0 };
});

await record('cancelled', async () => {
  const c = boot('two');
  await openDialog(c);
  c.press('Cancel');
  await wait(30);
  return { errors: c.errors, posts: c.posts(), label: c.label() };
});

await record('oneAccount', async () => {
  const c = boot('one');
  await openDialog(c);
  const before = { text: c.modalText(), buttons: c.modalButtons(), posts: c.posts().length };
  c.press('Sync 1 account');
  await wait(30);
  return { errors: c.errors, before, posts: c.posts() };
});

await record('heldWait', async () => {
  const c = boot('held');
  await openDialog(c);
  const before = { text: c.modalText(), buttons: c.modalButtons() };
  c.press('Wait, then sync');
  await wait(30);
  return { errors: c.errors, before, posts: c.posts() };
});

await record('heldAnyway', async () => {
  const c = boot('held');
  await openDialog(c);
  c.press('Sync anyway');
  await wait(30);
  return { errors: c.errors, posts: c.posts() };
});

/* A queue already running: the click is where to see it and stop it, and starts nothing. */
await record('runningClose', async () => {
  const c = boot('running');
  await openDialog(c);
  const before = { label: c.label(), text: c.modalText(), buttons: c.modalButtons() };
  c.press('Close');
  await wait(30);
  return { errors: c.errors, before, posts: c.posts() };
});

await record('runningStop', async () => {
  const c = boot('running');
  await openDialog(c);
  c.server.afterPost = 'two';
  c.press('Stop syncing the rest');
  await wait(50);
  return { errors: c.errors, posts: c.posts(), label: c.label(), toasts: c.toasts() };
});

/* The poll: a queue that ended on its own puts the label back and says so, once. */
await record('endedOnItsOwn', async () => {
  const c = boot('running');
  await wait(20);
  c.server.state = 'two';
  c.window.__applyAccountSync('a different signature');
  await wait(50);
  const first = { label: c.label(), toasts: c.toasts() };
  c.window.__applyAccountSync('a different signature again');
  await wait(50);
  return { errors: c.errors, first, toasts: c.toasts(), pageReads: c.pageReads() };
});

/* The button follows the list: a first account appearing brings it with no reload. */
await record('firstAccountAppears', async () => {
  STATES.empty = { html: read('empty.html'), info: STATES.two.info };
  const c = boot('empty');
  await wait(20);
  const before = c.label();
  c.server.state = 'two';
  c.window.__applyAccountSync('a different signature');
  await wait(50);
  return { errors: c.errors, before, label: c.label() };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
