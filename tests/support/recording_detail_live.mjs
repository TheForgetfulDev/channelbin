/* Drives the REAL recording page's live scripts in jsdom and prints what it observed as
   JSON. Called by tests/test_recording_page_numbers.py, which owns every assertion and
   every scenario - this file only replays them. Same shape as tests/support/dashboard_live.mjs.

   What is real: the markup the Flask route rendered for a live recording, its inline
   scripts, and util.js as shipped (inlined ahead of them, because the page's own scripts
   run while it parses and call util.js's helpers). Faked: EventSource (each scenario pushes
   its frames through the page's own onmessage), fetch (the page's 15s swap is answered with
   swap.html), timers (shortened), Date.now (a clock the scenario advances), and page reload,
   which jsdom reports as an unimplemented navigation.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html, swap.html and
   scenarios.json: {name: {frames: [{event, data}], advanceMs?, swap?: true answers the
   swap with swap.html instead of page.html, after?: [{event, data}] sent once the clock
   has moved, pageshow?: true fires a back-forward-cache restore at the end}}.

   Besides the screenshot tag it reports the live strip (its label, the reason after it,
   the stall and failure counts, whether it is marked not capturing) and how many times
   the page asked for an early swap through window.__refreshRecordingPage - counted apart
   from the 15s tick, which the shortened timers fire constantly. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const SWAP = fs.readFileSync(`${DIR}/swap.html`, 'utf8');
const SCENARIOS = JSON.parse(fs.readFileSync(`${DIR}/scenarios.json`, 'utf8'));
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const RID = Number(/const rid = (\d+);/.exec(PAGE)[1]);

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

async function run({ frames = [], advanceMs = 0, after = [], swap = false, pageshow = false }) {
  const errors = [];
  let reloads = 0;
  let clock = 1_700_000_000_000;
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) { reloads += 1; return; }
    errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const sources = [];
  const html = PAGE.replace('<head>', `<head><script>${UTIL}</script>`);
  const dom = new JSDOM(html, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `http://localhost:5000/recordings/${RID}`,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (target) => {
        const path = new URL(String(target), 'http://localhost:5000/').pathname;
        const isPage = path === `/recordings/${RID}`;
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: () => null },
          json: () => Promise.resolve({}),
          text: () => Promise.resolve(isPage ? (swap ? SWAP : PAGE) : '{}'),
          blob: () => Promise.reject(new Error('no image in jsdom')),
        });
      };
      w.EventSource = class {
        constructor(url) { this.url = url; sources.push(this); }
        close() {}
      };
      w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
      w.Date.now = () => clock;
      const realSetTimeout = w.setTimeout.bind(w);
      const realSetInterval = w.setInterval.bind(w);
      w.setTimeout = (fn, ms, ...args) => realSetTimeout(fn, Math.min(ms || 0, 2), ...args);
      w.setInterval = (fn, ms, ...args) => realSetInterval(fn, Math.min(ms || 0, 5), ...args);
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};

  const stream = sources.find((s) => s.url === `/api/stream/${RID}`);
  if (!stream) throw new Error(`the page opened no /api/stream/${RID} EventSource (${errors.join(' | ')})`);
  let earlySwaps = 0;
  const tick = window.__refreshRecordingPage;
  window.__refreshRecordingPage = () => { earlySwaps += 1; return tick && tick(); };
  const push = (f) => stream.onmessage({ data: JSON.stringify({ recording_id: RID, ...f }) });
  frames.forEach(push);
  await settle(20);
  clock += advanceMs;
  await settle(20);
  after.forEach(push);
  await settle(40);
  // The early swap waits a second of real time; the shortened setTimeout makes that 2 ms.
  await settle(20);
  if (pageshow) {
    window.dispatchEvent(new window.PageTransitionEvent('pageshow', { persisted: true }));
    await settle(20);
  }

  const tag = document.getElementById('live-thumb-tag');
  const text = document.getElementById('live-thumb-tag-text');
  const strip = document.getElementById('live-strip');
  const read = (id) => { const el = document.getElementById(id); return el ? el.textContent : null; };
  const out = {
    errors,
    reloads,
    earlySwaps,
    tag: text ? text.textContent : null,
    stale: tag ? tag.classList.contains('stale') : null,
    label: read('live-state'),
    why: read('live-state-why'),
    stalls: read('live-stalls'),
    failures: read('live-failures'),
    notCapturing: strip ? strip.classList.contains('not-capturing') : null,
  };
  window.close();
  return out;
}

const out = {};
for (const [name, scenario] of Object.entries(SCENARIOS)) {
  out[name] = await run(scenario);
}
process.stdout.write(JSON.stringify(out));
