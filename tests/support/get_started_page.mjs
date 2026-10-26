/* Drives the REAL Get started page in jsdom and prints what it observed as JSON. Called by
   tests/test_get_started_page_js.py, which owns every assertion - this file only reports.
   Same shape as tests/support/dashboard_sections.mjs.

   What it is for: the page promises that a first sync "moves on by itself", which is
   static/js/get-started.js swapping in a fresh server render when base.html's
   /api/nav-status poll reports a sync or recording signature the page was not rendered at
   (dev/changelog/1280). Every page and payload is what the Flask app answered at each
   state; util.js, get-started.js and base.html's inline poll are the shipped code. Faked:
   the network, which answers from whichever state the scenario says the server is in.

   argv: <fixture dir> <repo root>. The fixture dir holds <state>.html and <state>.json for
   each state in STATE_NAMES. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATE_NAMES = ['pending', 'syncing', 'synced'];
const STATES = {};
for (const s of STATE_NAMES) {
  STATES[s] = { html: read(`${s}.html`), nav: JSON.parse(read(`${s}.json`)) };
}

// duplicated from tests/support/dashboard_sections.mjs - each harness is standalone
const inlineScripts = (html) => html.replace(
  /<script src="[^"]*\/static\/js\/([\w.-]+\.js)[^"]*"><\/script>/g,
  (tag, name) => {
    const path = `${REPO}/static/js/${name}`;
    return fs.existsSync(path) ? `<script>${fs.readFileSync(path, 'utf8')}</script>` : '';
  });

const respond = (body, type) => Promise.resolve({
  ok: true,
  status: 200,
  headers: { get: () => type },
  json: () => (type === 'application/json' ? Promise.resolve(body) : Promise.reject(new Error('not json'))),
  text: () => Promise.resolve(type === 'application/json' ? JSON.stringify(body) : body),
});

function boot(start) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => { if (!/navigation/i.test(e.message)) errors.push(`jsdomError: ${e.message}`); });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const server = { state: start };
  const sent = [];
  const fetchStub = (target) => {
    const url = new URL(String(target), 'http://localhost:5000/');
    sent.push(url.pathname);
    if (url.pathname === '/get-started') return respond(STATES[server.state].html, 'text/html');
    if (url.pathname === '/api/nav-status') return respond(STATES[server.state].nav, 'application/json');
    return respond({ success: true }, 'application/json');
  };
  const dom = new JSDOM(inlineScripts(STATES[start].html), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/get-started',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      w.EventSource = class { close() {} };
      w.matchMedia = () => ({
        media: '', matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms));
  const view = () => ({
    sub: (document.getElementById('gs-sub') || { textContent: '' }).textContent.trim(),
    steps: Array.from(document.querySelectorAll('#gs-live .gs-step'))
      .map((li) => `${li.dataset.step}:${li.className.replace('gs-step ', '')}`),
    pageFetches: sent.filter((p) => p === '/get-started').length,
    navLinks: Array.from(document.querySelectorAll('.nav-link.nav-gs'))
      .map((a) => ({ shown: a.style.display !== 'none',
                     count: a.querySelector('.nav-count-setup').textContent.trim() })),
  });
  return {
    server, errors, view, settle,
    poll: async () => { window.fetchNavStatus(); await settle(); },
    close: () => window.close(),
  };
}

const out = {};

// The page's own first poll, at the state it was rendered at: nothing to fetch.
{
  const c = boot('pending');
  await c.settle();
  out.unchanged = { ...c.view(), errors: c.errors };
  c.close();
}

// The first sync starts, then finishes, with the page left open.
{
  const c = boot('pending');
  await c.settle();
  c.server.state = 'syncing';
  await c.poll();
  out.started = c.view();
  await c.poll();
  out.startedAgain = c.view();
  c.server.state = 'synced';
  await c.poll();
  out.finished = { ...c.view(), errors: c.errors };
  c.close();
}

process.stdout.write(JSON.stringify(out));
