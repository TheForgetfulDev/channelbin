/* Drives the REAL Dashboard's live badge updates in jsdom and prints what it observed as
   JSON. Called by tests/test_dashboard_live_js.py, which owns every assertion and every
   scenario - this file only replays them. Same shape as tests/support/maintenance_restart.mjs.

   Why it exists: which SSE frames move a row's badge is decided entirely in dashboard.js,
   and it was decided from a hand-kept list of event names that drifted from what app/
   actually publishes (dev/changelog/1023). No Python test can see that decision.

   What is real: the markup the Flask route rendered for seeded recordings, and util.js +
   dashboard.js evaluated as shipped. Faked: EventSource (each scenario pushes its frames
   through the page's own onmessage), fetch, timers (shortened), and page reload, which
   jsdom reports as an unimplemented navigation - that report IS the observation.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and scenarios.json:
   {name: {frames: [{recording_id, event, data}], watch: [recording ids]}}. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const SCENARIOS = JSON.parse(fs.readFileSync(`${DIR}/scenarios.json`, 'utf8'));
const JS = ['util.js', 'dashboard.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

async function run({ frames, watch }) {
  const errors = [];
  let reloads = 0;
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) { reloads += 1; return; }
    errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const sources = [];
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = () => Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => 'application/json' },
        json: () => Promise.resolve({ active: false }),
        text: () => Promise.resolve('{}'),
      });
      w.EventSource = class {
        constructor(url) { this.url = url; sources.push(this); }
        close() {}
      };
      w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
      const realSetTimeout = w.setTimeout.bind(w);
      w.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, Math.min(ms || 0, 2), ...args);
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  // As <script> elements rather than eval: dashboard.js reads util.js's top-level `const`s,
  // which only the shared global lexical scope of real scripts makes visible.
  JS.forEach((src) => {
    const el = document.createElement('script');
    el.textContent = src;
    document.body.appendChild(el);
  });
  document.dispatchEvent(new window.Event('DOMContentLoaded'));

  const stream = sources.find((s) => s.url === '/api/stream');
  if (!stream) throw new Error(`dashboard.js opened no /api/stream EventSource (${errors.join(' | ')})`);
  for (const f of frames) stream.onmessage({ data: JSON.stringify(f) });
  await new Promise((r) => setTimeout(r, 60));

  const rows = {};
  for (const rid of watch) {
    const badge = document.getElementById(`badge-${rid}`);
    const row = document.getElementById(`card-${rid}`);
    rows[rid] = {
      text: badge ? badge.textContent.trim() : null,
      cls: badge ? badge.className : null,
      status: row ? row.dataset.status : null,
    };
  }
  // A reload can only ever be one navigation per page; the harness counts attempts so a
  // stack of timers from repeated frames shows up as more than one.
  return { errors, reloads, rows };
}

const out = {};
for (const [name, sc] of Object.entries(SCENARIOS)) {
  try {
    out[name] = await run(sc);
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
}
process.stdout.write(JSON.stringify(out));
process.exit(0);
