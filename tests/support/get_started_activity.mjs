/* Drives the REAL Get started page through a load of the example channels in jsdom and
   prints what it observed as JSON. Called by tests/test_get_started_activity_js.py, which
   owns every assertion - this file only reports.

   What it is for: starting the load draws the progress card in place and brings it into
   view instead of reloading the page, and the card then follows the load by itself until
   it is done (dev/changelog/1396). Every page and fragment is what the Flask app answered at
   each state; util.js, get-started.js, examples.js and base.html's inline poll are the
   shipped code. Faked: the network, answering from whichever state the scenario says the
   server is in, and timers of a second or more, which run at once so the polls do not take
   real seconds.

   argv: <fixture dir> <repo root>. The fixture dir holds page-<state>.html and
   activity-<state>.html for each state in STATE_NAMES. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const STATE_NAMES = ['offer', 'syncing', 'testing', 'done'];
const STATES = {};
for (const s of STATE_NAMES) {
  STATES[s] = { page: read(`page-${s}.html`), activity: read(`activity-${s}.html`) };
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
  statusText: '200',
  headers: { get: () => type },
  json: () => (type === 'application/json' ? Promise.resolve(body) : Promise.reject(new Error('not json'))),
  text: () => Promise.resolve(type === 'application/json' ? JSON.stringify(body) : body),
});

const errors = [];
let navigations = 0;
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => {
  if (/navigation/i.test(e.message)) navigations += 1;
  else errors.push(`jsdomError: ${e.message}`);
});
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const server = { state: 'offer' };
const sent = [];
const scrolled = [];
const dom = new JSDOM(inlineScripts(STATES.offer.page), {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/get-started',
  virtualConsole: vc,
  beforeParse(w) {
    const realTimeout = w.setTimeout.bind(w);
    w.setTimeout = (fn, ms, ...a) => realTimeout(fn, ms >= 1000 ? 5 : ms, ...a);
    w.Element.prototype.scrollIntoView = function scrollIntoView() { scrolled.push(this.id); };
    w.fetch = (target, opts = {}) => {
      const url = new URL(String(target), 'http://localhost:5000/');
      const method = (opts.method || 'GET').toUpperCase();
      sent.push(`${method} ${url.pathname}`);
      if (method === 'POST' && url.pathname === '/api/examples') {
        server.state = 'syncing';
        return respond({ success: true, message: 'Loading.' }, 'application/json');
      }
      if (url.pathname === '/get-started') return respond(STATES[server.state].page, 'text/html');
      if (url.pathname === '/get-started/activity') return respond(STATES[server.state].activity, 'text/html');
      if (url.pathname === '/api/nav-status') return respond({}, 'application/json');
      return respond({ success: true }, 'application/json');
    };
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
const settle = (ms = 80) => new Promise((r) => setTimeout(r, ms));
const count = (what) => sent.filter((s) => s === what).length;
const view = () => {
  const card = document.getElementById('gs-activity');
  return {
    running: Boolean(card && card.dataset.running),
    steps: Array.from(document.querySelectorAll('#gs-activity .gs-run-step'))
      .map((li) => `${li.dataset.runStep}:${li.className.replace('gs-run-step ', '')}`),
    focused: document.activeElement ? document.activeElement.id : null,
    offer: Boolean(document.getElementById('gs-try')),
    pageFetches: count('GET /get-started'),
    activityFetches: count('GET /get-started/activity'),
    navigations,
  };
};

const out = {};
await settle();
out.before = view();

document.querySelector('[data-examples-load]').click();
await settle(20);
const confirmBtn = Array.from(document.querySelectorAll('.modal .modal-foot .btn')).pop();
confirmBtn.click();
await settle(30);
out.started = { ...view(), posts: count('POST /api/examples'), scrolled: [...scrolled] };

server.state = 'testing';
await settle();
out.testing = view();

server.state = 'done';
await settle();
out.done = view();

await settle(150);
out.afterDone = { ...view(), errors };
window.close();
process.stdout.write(JSON.stringify(out));
