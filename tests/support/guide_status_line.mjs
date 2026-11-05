/* Drives the REAL /guide page and a REAL channel page in jsdom, one fresh page per case, and
   reports the count line static/js/guide.js writes for that case's real /api/guide/epg
   payload, as JSON. Called by tests/test_guide_status_line_js.py, which owns every assertion
   (dev/changelog/1436).

   argv: <fixture dir> <repo root>. The fixture dir holds guide.html, channel.html and
   cases.json: {name: {page, epg, text, dur_max}}, the last two being the filter to apply
   after the first render. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const CASES = JSON.parse(fs.readFileSync(`${DIR}/cases.json`, 'utf8'));
const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

async function runCase(spec) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const fetchStub = (target) => {
    const payload = String(target).includes('/api/guide/epg') ? spec.epg : { success: true, results: [] };
    return Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload),
      text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  // Only the page's inline scripts run from the markup: its <script src> tags are not loaded,
  // so the two files under test are evaluated by hand below.
  const dom = new JSDOM(fs.readFileSync(`${DIR}/${spec.page}.html`, 'utf8'), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      // /guide's inline boot script calls this before guide.js is evaluated below; the real
      // one replaces this stub and is called by hand once it exists.
      w.fetchAndRender = function () {};
      Object.defineProperty(w, 'innerWidth', { value: 1440, configurable: true });
      w.matchMedia = (query) => ({
        media: query, matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  const doc = window.document;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  // jsdom fires DOMContentLoaded after this constructor returns, so the scripts evaluated
  // here register for it and boot exactly as they do in a browser.
  window.eval(JS.join('\n'));
  window.fetchAndRender();

  for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0));

  const slots = doc.querySelectorAll('#guide-program-rows .guide-program.is-dummy').length;
  if (spec.text) doc.getElementById('guide-search').value = spec.text;
  if (spec.dur_max) {
    doc.getElementById('dur-filter-unit').value = 'minutes';
    doc.getElementById('dur-filter-max').value = String(spec.dur_max);
  }
  if (spec.text || spec.dur_max) window.renderGuide();

  const result = {
    errors,
    slots_drawn: slots,
    line: doc.getElementById('guide-sb-counts').textContent.replace(/\s+/g, ' ').trim(),
    empty_state: !doc.getElementById('guide-empty').hidden,
  };
  window.close();
  return result;
}

const out = {};
for (const [name, spec] of Object.entries(CASES)) out[name] = await runCase(spec);
console.log(JSON.stringify(out));
process.exit(0);
