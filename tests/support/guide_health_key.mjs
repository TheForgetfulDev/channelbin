/* Drives the REAL /guide page in jsdom and reports the key static/js/guide.js writes for the
   channel column's health dots, as JSON. Called by tests/test_guide_health_key_js.py, which
   owns every assertion. Same boot as tests/support/guide_tech_line.mjs.

   Why it exists: dev/changelog/1290 - at phone width the channel column carries a band's
   colour as a dot and nothing else, and a first-time user saw orange, grey and green dots
   with nothing saying what they meant.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and cases.json (a list of
   channel dicts per case, keyed by case name). */
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
    w.fetchAndRender = function () {};
    Object.defineProperty(w, 'innerWidth', { value: 375, configurable: true });
    w.matchMedia = (query) => ({
      media: query, matches: true, onchange: null,
      addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, dispatchEvent() { return false; },
    });
  },
});
const { window } = dom;
window.scrollTo = () => {};
window.eval(JS.join('\n'));

const host = window.document.getElementById('guide-sb-healthkey');
const out = { errors, host_present: Boolean(host), host_html_at_boot: host ? host.innerHTML : null };
for (const [name, channels] of Object.entries(CASES)) {
  window.renderHealthKey(channels);
  out[name] = {
    bands: Array.from(host.querySelectorAll('[data-band]')).map((el) => el.dataset.band),
    names: Array.from(host.querySelectorAll('[data-band]')).map((el) => el.textContent.trim()),
    dot_classes: Array.from(host.querySelectorAll('.guide-legend-dot')).map((el) => el.className),
    text: host.textContent.trim(),
  };
}
console.log(JSON.stringify(out));
process.exit(0);
