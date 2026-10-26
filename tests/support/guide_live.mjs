/* Drives the REAL /guide page in jsdom and reports, as JSON, what it did when base.html's
   /api/nav-status poll said a recording had started. Called by tests/test_guide_live_js.py,
   which owns every assertion - this file only reports.

   Why it exists: dev/docs/BUGS.md 2026-10-23 06:36:01 AM and 06:36:02 AM. The guide re-read its grid only every five
   minutes, so a recording that started left its cell saying SCHEDULED, and the sidebar's
   Recordings count was filled in at render and never again, so it said 0 through the whole
   recording. Both now follow the nav poll, and both halves happen in the browser.

   Real: the page and both nav-status payloads are what the Flask app answered before and
   after a recording row was created; base.html's own inline poll calls the hooks; util.js
   and guide.js are the shipped files. Faked: the network, which answers nav-status from
   whichever state the scenario says the server is in and the EPG grid with no channels -
   what is counted is whether the grid was asked for again, not what it drew.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html, nav-before.json and
   nav-after.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const PAGE = read('page.html');
const NAV = { before: JSON.parse(read('nav-before.json')), after: JSON.parse(read('nav-after.json')) };
const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const server = { state: 'before' };
  const sent = [];
  const fetchStub = (target) => {
    const href = String(target);
    sent.push(href);
    let payload = [];
    if (href.includes('/api/nav-status')) payload = NAV[server.state];
    else if (href.includes('/api/guide/epg')) payload = { channels: [] };
    return Promise.resolve({
      ok: true, status: 200, headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload), text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/guide',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      // guide.html's tail script names fetchAndRender before guide.js defines it; see
      // tests/support/guide_modal.mjs for why a no-op stands in until the eval below.
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
  // One eval, so guide.js sees util.js's top-level consts (tests/support/guide_modal.mjs).
  window.eval(JS.join('\n'));
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const c = {
    window, document, server, errors,
    settle: (ms = 60) => new Promise((r) => setTimeout(r, ms)),
    poll: async () => { window.fetchNavStatus(); await c.settle(); },
    gridFetches: () => sent.filter((h) => h.includes('/api/guide/epg')).length,
    recCount: () => (document.querySelector('.nav-count-recordings') || {}).textContent,
  };
  return c;
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

await record('recording_starts', async () => {
  const c = boot();
  await c.settle();
  await c.poll();
  const atBoot = { gridFetches: c.gridFetches(), recCount: c.recCount() };
  c.server.state = 'after';
  await c.poll();
  const afterChange = { gridFetches: c.gridFetches(), recCount: c.recCount() };
  await c.poll();
  return {
    errors: c.errors,
    hook: typeof c.window.__applyRecordingSignature,
    atBoot,
    afterChange,
    afterNextPoll: { gridFetches: c.gridFetches() },
  };
});

await record('record_dialog_open', async () => {
  const c = boot();
  await c.settle();
  await c.poll();
  const atBoot = c.gridFetches();
  c.document.getElementById('record-modal').style.display = 'flex';
  c.server.state = 'after';
  await c.poll();
  const whileOpen = c.gridFetches();
  c.document.getElementById('record-modal').style.display = 'none';
  await c.poll();
  return { errors: c.errors, atBoot, whileOpen, afterClose: c.gridFetches() };
});

console.log(JSON.stringify(out));
// /guide's own intervals keep the event loop alive otherwise.
process.exit(0);
