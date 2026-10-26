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

// A COMPLETED recording the server called partial (app/recording_story.py::partial_note),
// beside the same status with no sentence: the mark must change only when there is one.
const PARTIAL = '3m captured of 1h 20m scheduled.';
const partialHolder = window.document.createElement('div');
partialHolder.innerHTML = window.recStatusBadge('COMPLETED', PARTIAL);
const pnode = partialHolder.firstElementChild;
const partial = {
  sentence: PARTIAL,
  text: pnode ? pnode.textContent.trim() : null,
  tip: pnode ? pnode.getAttribute('data-tip') : null,
  badgeClass: pnode ? pnode.className : null,
  cls: window.recCssClass('COMPLETED', PARTIAL),
  wholeCls: window.recCssClass('COMPLETED', null),
  // A partial sentence on a status that is not finished must not change that status's mark.
  liveCls: window.recCssClass('IN_PROGRESS', PARTIAL),
};

// A COMPLETED recording the user stopped early (app/recording_story.py::stopped_early_note):
// said, but not in the partial chip's amber (dev/changelog/1287).
const STOPPED = 'You stopped it after 2m 35s of 8m scheduled.';
const stoppedHolder = window.document.createElement('div');
stoppedHolder.innerHTML = window.recStatusBadge('COMPLETED', null, STOPPED);
const snode = stoppedHolder.firstElementChild;
const stopped = {
  sentence: STOPPED,
  text: snode ? snode.textContent.trim() : null,
  tip: snode ? snode.getAttribute('data-tip') : null,
  badgeClass: snode ? snode.className : null,
  cls: window.recCssClass('COMPLETED', null),
  // A partial sentence outranks it: something else went missing too.
  bothText: (() => {
    const h = window.document.createElement('div');
    h.innerHTML = window.recStatusBadge('COMPLETED', PARTIAL, STOPPED);
    return h.firstElementChild ? h.firstElementChild.textContent.trim() : null;
  })(),
  liveText: (() => {
    const h = window.document.createElement('div');
    h.innerHTML = window.recStatusBadge('IN_PROGRESS', null, STOPPED);
    return h.firstElementChild ? h.firstElementChild.textContent.trim() : null;
  })(),
};

// A SCHEDULED recording whose start time has come reads Starting, not Scheduled, until the
// recorder flips it (dev/changelog/1290); and a cell too narrow for glyph and word keeps the
// glyph alone, with the word in its label.
const badge = (...args) => {
  const h = window.document.createElement('div');
  h.innerHTML = window.recStatusBadge(...args);
  const n = h.firstElementChild;
  return n ? { text: n.textContent.trim(), tip: n.getAttribute('data-tip'),
               label: n.getAttribute('aria-label') } : null;
};
const isoAt = (minutes) => new Date(Date.now() + minutes * 60000).toISOString().slice(0, 19);
const starting = {
  past: window.recStarting('SCHEDULED', isoAt(-1)),
  future: window.recStarting('SCHEDULED', isoAt(30)),
  running: window.recStarting('IN_PROGRESS', isoAt(-1)),
  none: window.recStarting('SCHEDULED', null),
  chip: badge('SCHEDULED', null, null, true),
  plain: badge('SCHEDULED', null, null, false),
  liveIgnoresIt: badge('IN_PROGRESS', null, null, true),
};
const narrow = {
  wide: badge('IN_PROGRESS', null, null, false, 200),
  narrow: badge('IN_PROGRESS', null, null, false, 50),
  unknownWidth: badge('IN_PROGRESS'),
  noGlyphNarrow: badge('SCHEDULED', null, null, false, 50),
};

console.log(JSON.stringify({ errors: bootErrors, vocab, chips, unknown, partial, stopped,
                             starting, narrow }));
// /guide's own setInterval(updateNowLine, ...) / setInterval(fetchAndRender, ...) keep the
// event loop alive forever otherwise - this harness never needs them to fire.
process.exit(0);
