/* Drives the REAL /guide page in jsdom and reports what static/js/guide.js writes under a
   channel's name when it has no stream details, as JSON. Called by
   tests/test_guide_tech_line_js.py, which owns every assertion.

   Why it exists: dev/changelog/1276 - the line read a bare "no stream data" for every case
   and a first-time user, seeing it beside the `--` badge, could not tell what either meant.
   It now says why: never tested, the last check failed or was cancelled, or a check passed
   and measured nothing. Same boot as tests/support/guide_rec_chip.mjs.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and cases.json (one
   channel dict per case, keyed by case name). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const CASES = JSON.parse(fs.readFileSync(`${DIR}/cases.json`, 'utf8'));

const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const fetchStub = (target) => {
  const payload = String(target).includes('/api/guide/epg') ? { channels: [] } : [];
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};

const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/guide',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = fetchStub;
    // guide.html's tail inline script references fetchAndRender before guide.js has defined
    // it; jsdom never fetches a <script src>, so the stub stands in until the eval below.
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
window.scrollTo = () => {};
// One eval call, not one per file: guide.js reads util.js's top-level `const` bindings at
// eval time, and separate window.eval() calls do not share a lexical scope in jsdom.
window.eval(JS.join('\n'));

const out = {};
for (const [name, ch] of Object.entries(CASES)) {
  const box = window.document.createElement('div');
  box.className = 'guide-channel-tech';
  box.dataset.channelId = String(ch.id);
  window.document.body.appendChild(box);
  window.renderTechReadout(ch);
  const span = box.querySelector('.guide-tech');
  out[name] = {
    row_text: box.textContent.trim(),
    row_tip: span ? span.getAttribute('data-tip') : null,
    sheet_text: window.channelTechText(ch),
  };
  box.remove();
}

console.log(JSON.stringify({ errors, cases: out }));
process.exit(0);
