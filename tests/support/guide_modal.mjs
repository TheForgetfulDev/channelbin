/* Drives the REAL /guide page in jsdom and prints what it observed as JSON.
   Called by tests/test_guide_modal_padding_js.py, which owns every assertion - this file
   only reports, so a failure reads as "the modal did X" in Python rather than as a node
   exit code. Same shape as tests/support/logs_page.mjs.

   Why it exists: dev/docs/BUGS.md 2026-08-06 - static/js/guide.js's applyProfilePadding()
   unconditionally overwrote a hand-typed Start/Stop field whenever the Recording Profile
   dropdown changed afterward, with no check for a prior manual edit. That is pure
   client-side DOM/event behavior Python cannot reach.

   What is real here and what is not: the markup is what the Flask /guide route really
   rendered (a seeded channel in the guide plus a real RecordingProfile row), and util.js +
   guide.js are the shipped files, evaluated into the window the way their <script src>
   would have. Faked: the network (savedSearches and the EPG grid fetch are both stubbed to
   empty responses) - the test drives the modal directly via window.openModal(prog, ch) with
   a synthetic program object, the same shape the real EPG API returns, rather than routing
   through a live grid cell click. jsdom computes no layout, so this cannot say anything
   about geometry - only about which field held which value after which event.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');

const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const fetchStub = (target) => {
    const href = String(target);
    const payload = href.includes('/api/guide/epg') ? { channels: [] } : [];
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
      // guide.html's tail inline script assigns `window.onManualRecordSaved =
      // fetchAndRender` before guide.js (a <script src>, which jsdom never fetches) has
      // defined it - a real browser's document-order script loading means this is never
      // undefined there. A no-op stub satisfies the reference; guide.js's real
      // `function fetchAndRender` declaration below overwrites it once evaluated.
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
  const { document } = window;
  window.scrollTo = () => {};

  // One eval call, not one per file: guide.js's top-level `let PIXELS_PER_MINUTE = ...`
  // reads util.js's top-level `const PX_PER_MIN_DESKTOP` at eval time, and separate
  // window.eval() calls do not share a top-level lexical (const/let) scope in jsdom the
  // way separate <script> tags do in a real browser - only var/function declarations
  // (which attach directly to the global object) survive across separate eval() calls.
  window.eval(JS.join('\n'));
  // guide.js wires its modal listeners inside a `DOMContentLoaded` handler, registered
  // only once we eval it above - well after jsdom's real DOMContentLoaded already fired
  // during initial parse. Firing a synthetic one now invokes it, the same as a real
  // browser would have for a <script src> loaded before the event.
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const $ = (s) => document.querySelector(s);
  const type = (el, v) => { el.value = v; el.dispatchEvent(new window.Event('input', { bubbles: true })); };
  const change = (el, v) => { el.value = v; el.dispatchEvent(new window.Event('change', { bubbles: true })); };

  return {
    window, document, errors, $,
    openModal: (prog, ch) => window.openModal(prog, ch),
    typeStart: (v) => type($('#modal-start'), v),
    typeStop: (v) => type($('#modal-stop'), v),
    selectProfile: (v) => change($('#modal-profile'), v),
    startValue: () => $('#modal-start').value,
    stopValue: () => $('#modal-stop').value,
    noteText: () => (($('#modal-padding-note') || {}).textContent || '').trim(),
    noteVisible: () => $('#modal-padding-note').style.display !== 'none',
  };
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

// A program starting 2026-08-10T20:00 UTC / ending 21:00 UTC, deliberately far from "now"
// so no now-line/scroll logic in openModal can interfere.
const PROG = {
  id: 501, title: 'Test Program', suggested_name: 'Test Program',
  start_time: '2026-08-10T20:00:00', stop_time: '2026-08-10T21:00:00',
  stream_url: 'http://example.test/live/1', channel_id: 9, group_id: null,
  has_recording: false,
};
const CH = { id: 9, default_profile_id: null };

/* ── Baseline: no manual edit, changing profile still recomputes both fields ──────── */
await record('no_manual_edit', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  const openedStart = c.startValue();
  const openedStop = c.stopValue();
  c.selectProfile('1');
  return {
    errors: c.errors,
    openedStart, openedStop,
    start: c.startValue(), stop: c.stopValue(),
    noteText: c.noteText(), noteVisible: c.noteVisible(),
  };
});

/* ── The bug: hand-typed start, then a profile change with NO start padding at all ─── */
await record('hand_typed_start_survives_profile_change', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  const openedStop = c.stopValue();
  c.typeStart('2026-08-10T20:30');
  c.selectProfile('1'); // profile 1 = 0 pre-padding, 15 post-padding, per the Python fixture
  return {
    errors: c.errors,
    openedStop,
    start: c.startValue(), stop: c.stopValue(),
    noteText: c.noteText(), noteVisible: c.noteVisible(),
  };
});

/* ── Both fields hand-edited: profile change must leave both alone ────────────────── */
await record('both_hand_edited', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  c.typeStart('2026-08-10T20:30');
  c.typeStop('2026-08-10T20:45');
  c.selectProfile('1');
  return {
    errors: c.errors,
    start: c.startValue(), stop: c.stopValue(),
    noteText: c.noteText(), noteVisible: c.noteVisible(),
  };
});

/* ── Reopening the modal for a fresh program clears the edited-flag ────────────────── */
await record('reopen_clears_edit_flag', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  c.typeStart('2026-08-10T20:30');
  c.selectProfile('1');
  const afterFirstEdit = c.startValue();
  const prog2 = { ...PROG, id: 502, start_time: '2026-08-11T20:00:00', stop_time: '2026-08-11T21:00:00' };
  c.openModal(prog2, CH);
  c.selectProfile('1');
  return {
    errors: c.errors,
    afterFirstEdit,
    reopenedStart: c.startValue(),
  };
});

console.log(JSON.stringify(out));
// /guide's own setInterval(updateNowLine, ...) / setInterval(fetchAndRender, ...) keep the
// event loop alive forever otherwise - this harness never needs them to fire.
process.exit(0);
