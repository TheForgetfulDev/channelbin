/* Drives the REAL /guide page in jsdom and reports the recording chip static/js/guide.js
   draws for every Recording.status, as JSON. Called by tests/test_guide_rec_chip_js.py,
   which owns every assertion - this file only reports, so a failure reads as "the guide
   labelled JOINING as Recorded" in Python rather than as a node exit code. Same shape as
   tests/support/guide_modal.mjs.

   Why it exists: dev/changelog/1083 - guide.js spelled its own status->label table, so
   CONCATENATING and ANALYZING both rendered "✓ Recorded" while CONVERTING, the phase after
   them, rendered "✓ Converting". The word now comes from the server's vocabulary through
   base.html's meta tag and util.js::recStatusLabel, and that whole path is browser-side:
   Python can see the meta tag but not what guide.js does with it.

   What is real here and what is not: the markup is what the Flask /guide route really
   rendered, and util.js + guide.js are the shipped files, evaluated into the window the way
   their <script src> would have. Faked: the network (savedSearches and the EPG grid fetch
   are stubbed to empty responses) - the chips are read by calling recStatusBadge() directly
   rather than by rendering a seeded recording into a grid cell, because the status set under
   test includes two the EPG route deliberately never sends.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and statuses.json (the
   status list, from app/fmt_utils.py - node must not re-type it). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const STATUSES = JSON.parse(fs.readFileSync(`${DIR}/statuses.json`, 'utf8'));

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

// Whatever the meta tag actually carried into the page, so Python can check the browser
// read the server's table rather than that it merely rendered one.
const vocab = window.recStatusVocab();

// Everything the page complained about while booting, before this harness deliberately
// hands it an unknown status below and expects a complaint.
const bootErrors = errors.slice();

const chips = {};
for (const status of STATUSES) {
  const before = errors.length;
  const html = window.recStatusBadge(status);
  const holder = window.document.createElement('div');
  holder.innerHTML = html;
  const node = holder.firstElementChild;
  chips[status] = {
    html,
    cls: window.recCssClass(status),
    text: node ? node.textContent.trim() : null,
    tip: node ? node.getAttribute('data-tip') : null,
    badgeClass: node ? node.className : null,
    complained: errors.length > before,
  };
}

// A status the table knows nothing about: the chip must render nothing and say so, rather
// than borrow the last branch's colour and word.
const unknownBefore = errors.length;
const unknown = {
  html: window.recStatusBadge('INVENTED_STATUS'),
  cls: window.recCssClass('INVENTED_STATUS'),
  complained: false,
};
unknown.complained = errors.length > unknownBefore;

console.log(JSON.stringify({ errors: bootErrors, vocab, chips, unknown }));
// /guide's own setInterval(updateNowLine, ...) / setInterval(fetchAndRender, ...) keep the
// event loop alive forever otherwise - this harness never needs them to fire.
process.exit(0);
