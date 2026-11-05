/* Drives the REAL Readiness page's "Sync N accounts now" button in jsdom and prints what it
   observed as JSON. Called by tests/test_readiness_sync_queue_js.py, which owns every
   assertion - this file only reports.

   What is real: the markup /readiness rendered, the payload readiness.evaluate() built over
   stale accounts, and util.js + sync-queue.js + readiness.js as shipped. Faked: the network, which records
   every request, and the poll timer, which is shortened.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and payload.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const html = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const payload = JSON.parse(fs.readFileSync(`${DIR}/payload.json`, 'utf8'));
const JS = ['util.js', 'sync-queue.js', 'readiness.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const QUEUE = '/api/accounts/sync-queue';

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/* `conflicts` is what the status read answers; `payloads` answers /api/readiness in order, the
   last one repeating. */
function boot({ conflicts = [], payloads = [payload] } = {}) {
  const errors = [];
  const requests = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  let reads = 0;
  const respond = (body) => Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/readiness',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (target, init = {}) => {
        const href = String(target);
        if (href.includes('/api/readiness')) {
          const answer = payloads[Math.min(reads, payloads.length - 1)];
          reads += 1;
          return respond(answer);
        }
        requests.push({ href, method: init.method || 'GET', body: init.body || null });
        if (href.includes(QUEUE) && (init.method || 'GET') === 'GET') {
          return respond({ success: true, queue: null, conflicts, pause_seconds: 60 });
        }
        return respond({ success: true, message: 'Syncing 2 accounts, one at a time.' });
      };
      const realSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, Math.min(ms || 0, 5), ...args);
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  JS.forEach((src) => window.eval(src));
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  return {
    errors,
    requests,
    reads: () => reads,
    click,
    capRow: (id) => $$(`.rd-caprow[data-cap="${id}"]`)[0],
    fixButton: () => $$('[data-fix="account_sync"]')[0],
    modalText: () => {
      const panels = $$('.modal-panel');
      return panels.length ? panels[panels.length - 1].textContent.replace(/\s+/g, ' ').trim() : '';
    },
    modalButtons: () => $$('.modal-panel button').map((b) => b.textContent.trim()).filter(Boolean),
    button: (label) => $$('.modal-panel button').find((b) => b.textContent.trim() === label),
    toasts: () => $$('.toast').map((t) => t.textContent.replace(/\s+/g, ' ').trim()),
    posts: () => requests.filter((r) => r.method === 'POST'),
  };
}

/* The button sits on the capability's summary row only when it is that row's one problem, so
   open the capability and use the check's own row. */
const openDialog = async (c) => {
  await wait(30);
  const cap = payload.capabilities.find((k) => k.needs.includes('account_sync'));
  c.click(c.capRow(cap.id));
  await wait(10);
  c.click(c.fixButton());
  await wait(30);
};

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

await record('clear', async () => {
  const c = boot();
  await openDialog(c);
  const before = { text: c.modalText(), buttons: c.modalButtons(), posts: c.posts().length };
  c.click(c.button('Sync 2 accounts'));
  await wait(30);
  return { errors: c.errors, before, posts: c.posts(), toasts: c.toasts() };
});

await record('cancelled', async () => {
  const c = boot();
  await openDialog(c);
  c.click(c.button('Cancel'));
  await wait(30);
  return { errors: c.errors, posts: c.posts() };
});

await record('heldWait', async () => {
  const c = boot({ conflicts: ['A recording is in progress ("Big Game").'] });
  await openDialog(c);
  const before = { text: c.modalText(), buttons: c.modalButtons() };
  c.click(c.button('Wait, then sync'));
  await wait(30);
  return { errors: c.errors, before, posts: c.posts() };
});

await record('heldAnyway', async () => {
  const c = boot({ conflicts: ['A recording is in progress ("Big Game").'] });
  await openDialog(c);
  c.click(c.button('Sync anyway'));
  await wait(30);
  return { errors: c.errors, posts: c.posts() };
});

/* While the server says a queue runs, the page re-reads itself; when it stops, it says so and
   stops polling. */
await record('following', async () => {
  const active = { ...payload, sync_queue_active: true };
  const done = { ...payload, sync_queue_active: false };
  const c = boot({ payloads: [active, active, active, done] });
  // Each re-read renders the page, which takes as long as the box allows, so this waits for
  // the fourth read rather than guessing how long three renders take.
  const deadline = Date.now() + 20000;
  while (c.reads() < 4 && Date.now() < deadline) await wait(10);
  await wait(10);
  const reads = c.reads();
  await wait(50);
  return { errors: c.errors, reads, readsLater: c.reads(), toasts: c.toasts() };
});

/* No queue: a single read, and no toast. */
await record('idle', async () => {
  const c = boot();
  await wait(50);
  return { errors: c.errors, reads: c.reads(), toasts: c.toasts() };
});

process.stdout.write(JSON.stringify(out));
process.exit(0);
