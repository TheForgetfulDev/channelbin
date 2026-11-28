/* Evaluates the REAL static/js/util.js and static/js/guide.js in jsdom and reports what
   renderLifecycleBadge() draws into a channel row's pill slot, and what replacementHint()
   says on each surface, for each case's row as /api/guide/epg sends it. JSON out. Called by
   tests/test_guide_lifecycle_pill_js.py, which owns every assertion
   (DESIGN-channel-successors.md section 9, dev/changelog/1801).

   argv: <cases.json> <repo root>. cases.json: {name: row}. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , CASES_PATH, REPO] = process.argv;
const CASES = JSON.parse(fs.readFileSync(CASES_PATH, 'utf8'));
const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const ids = Object.values(CASES).map((row) => row.id);
const body = ids.map((id) => `<div class="guide-channel-row" data-channel-id="${id}">` +
  `<span class="guide-lifecycle-badge" data-channel-id="${id}"></span></div>` +
  `<div class="guide-row" data-channel-id="${id}"></div>`).join('');
const dom = new JSDOM(`<!doctype html><html><head></head><body>${body}</body></html>`, {
  runScripts: 'dangerously',
  url: 'http://localhost:5000/',
  virtualConsole: vc,
  beforeParse(w) {
    // What /guide's inline boot block sets before guide.js loads; nothing here reads more.
    w.GUIDE_CONFIG = {};
    w.matchMedia = (query) => ({
      media: query, matches: false, onchange: null,
      addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, dispatchEvent() { return false; },
    });
  },
});
const { window } = dom;
window.eval(JS.join('\n'));

const out = {};
for (const [name, row] of Object.entries(CASES)) {
  window.renderLifecycleBadge(row);
  const pill = window.document.querySelector(`.guide-lifecycle-badge[data-channel-id="${row.id}"] .qbadge`);
  out[name] = {
    pill: pill ? pill.textContent : null,
    tip: pill ? pill.getAttribute('data-tip') : null,
    dimmed: window.document.querySelector(`.guide-row[data-channel-id="${row.id}"]`)
      .classList.contains('is-removed'),
    sheet_hint: window.replacementHint(row, true),
  };
}
process.stdout.write(JSON.stringify({ cases: out, errors }));
