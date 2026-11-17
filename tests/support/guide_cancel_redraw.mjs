/* Drives the REAL /guide page in jsdom through scheduling's undo - Cancel recording on a
   SCHEDULED program - and reports, as JSON, what the program's cell said afterwards.
   Called by tests/test_guide_cancel_redraw_js.py, which owns every assertion.

   Real: the page, and the two /api/guide/epg payloads the Flask app answered with the
   recording present and after it was deleted; util.js and guide.js are the shipped files.
   Faked: the network. The grid answers from whichever state the scenario has the server in,
   and POST .../cancel-json moves it to `after`, as the real route deletes the row before it
   answers. A scenario can hold one grid answer back to put two in flight at once.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html, epg-before.json and
   epg-after.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGE = read('page.html');
const EPG = { before: JSON.parse(read('epg-before.json')), after: JSON.parse(read('epg-after.json')) };
const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const server = { state: 'before', hold: null };
  const answer = (payload) => ({
    ok: true, status: 200, headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload), text: () => Promise.resolve(JSON.stringify(payload)),
  });
  const fetchStub = (target, init = {}) => {
    const href = String(target);
    if (href.includes('/cancel-json') && (init.method || '').toUpperCase() === 'POST') {
      server.state = 'after';
      return Promise.resolve(answer({ success: true }));
    }
    if (href.includes('/api/guide/epg')) {
      const payload = EPG[server.state];
      // A held answer is the one a slow request returns late: it carries the state the
      // server was in when the request was made.
      if (server.hold) {
        const release = server.hold;
        server.hold = null;
        return new Promise((resolve) => { release.push(() => resolve(answer(payload))); });
      }
      return Promise.resolve(answer(payload));
    }
    return Promise.resolve(answer([]));
  };

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/guide',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.fetchAndRender = function () {};
      Object.defineProperty(w, 'innerWidth', { value: 1280, configurable: true });
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
  window.HTMLElement.prototype.scrollIntoView = () => {};
  fakeLayout(window);
  window.eval(JS.join('\n'));
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const settle = (ms = 80) => new Promise((r) => setTimeout(r, ms));
  return {
    window, document, server, errors, settle,
    scheduledCells: () => document.querySelectorAll('.rec-scheduled').length,
    program: () => EPG.before.channels.flatMap((ch) => (ch.programs || []).map((p) => ({ p, ch })))
      .find(({ p }) => p.has_recording),
    cancel: async () => {
      document.getElementById('modal-delete').click();
      await settle(20);
      const confirmBtn = [...document.querySelectorAll('.modal:not([id]) .modal-foot .btn')]
        .find((b) => b.textContent.trim() === 'Cancel recording');
      confirmBtn.click();
      await settle();
    },
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

await record('cancel', async () => {
  const c = boot();
  await c.settle();
  const atBoot = c.scheduledCells();
  const { p, ch } = c.program();
  c.window.openModal(p, ch);
  await c.settle(20);
  await c.cancel();
  return { errors: c.errors, atBoot, afterCancel: c.scheduledCells() };
});

// An older grid request (the 5-minute refresh, a nav-poll re-read) still in flight when the
// cancel's own re-read answers: whichever answers LAST is drawn.
await record('cancel_with_older_fetch_in_flight', async () => {
  const c = boot();
  await c.settle();
  const atBoot = c.scheduledCells();
  const release = [];
  c.server.hold = release;
  c.window.fetchAndRender();
  const { p, ch } = c.program();
  c.window.openModal(p, ch);
  await c.settle(20);
  await c.cancel();
  const afterCancel = c.scheduledCells();
  release.forEach((r) => r());
  await c.settle();
  return { errors: c.errors, atBoot, afterCancel, afterLateAnswer: c.scheduledCells() };
});

console.log(JSON.stringify(out));
process.exit(0);
