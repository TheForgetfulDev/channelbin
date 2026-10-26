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

  const nameRequests = [];
  const fetchStub = (target, init) => {
    const href = String(target);
    let payload = href.includes('/api/guide/epg') ? { channels: [] } : [];
    // The record modal's rename for a picked profile (dev/changelog/1161): answered with a
    // name that says which profile asked, so a stale or wrong answer is visible.
    if (href.includes('/api/guide/suggested-name')) {
      const body = JSON.parse(init.body);
      nameRequests.push(body);
      payload = { success: true, name: `Renamed for ${body.profile_id}` };
    }
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
    window, document, errors, $, nameRequests,
    typeName: (v) => type($('#modal-name'), v),
    nameValue: () => $('#modal-name').value,
    openModal: (prog, ch, opts) => window.openModal(prog, ch, opts),
    typeStart: (v) => type($('#modal-start'), v),
    typeStop: (v) => type($('#modal-stop'), v),
    selectProfile: (v) => change($('#modal-profile'), v),
    startValue: () => $('#modal-start').value,
    stopValue: () => $('#modal-stop').value,
    noteText: () => (($('#modal-padding-note') || {}).textContent || '').trim(),
    noteVisible: () => $('#modal-padding-note').style.display !== 'none',
    lateText: () => (($('#modal-late-note') || {}).textContent || '').trim(),
    lateVisible: () => Boolean($('#modal-late-note')) && $('#modal-late-note').style.display !== 'none',
    labels: () => ({ title: $('#modal-title').textContent.trim(),
                     submit: $('#modal-submit').textContent.trim() }),
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

const settle = (w) => new Promise((r) => w.setTimeout(r, 0));

/* ── Picking a profile renames the showing with that profile's naming ───────────────── */
await record('profile_change_renames', async () => {
  const c = boot();
  c.openModal({ ...PROG, sub_title: 'Pilot', category: 'Drama', channel_name: 'Chan' }, CH);
  const opened = c.nameValue();
  c.selectProfile('1');
  await settle(c.window);
  await settle(c.window);
  const afterPick = c.nameValue();
  c.selectProfile('');
  await settle(c.window);
  await settle(c.window);
  return { errors: c.errors, opened, afterPick, afterNone: c.nameValue(),
           requests: c.nameRequests };
});

/* ── A typed name is the user's: a later profile pick leaves it alone ─────────────────── */
await record('typed_name_survives_profile_change', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  c.typeName('My own name');
  c.selectProfile('1');
  await settle(c.window);
  await settle(c.window);
  return { errors: c.errors, name: c.nameValue(), requests: c.nameRequests };
});

/* ── Editing a scheduled recording never renames it ──────────────────────────────────── */
await record('edit_does_not_rename', async () => {
  const c = boot();
  c.openModal({ ...PROG, has_recording: true, recording_id: 77, recording_status: 'SCHEDULED',
                recording_start_time: PROG.start_time, recording_stop_time: PROG.stop_time,
                recording_profile_id: null, suggested_name: 'Stored name' }, CH);
  c.selectProfile('1');
  await settle(c.window);
  await settle(c.window);
  return { errors: c.errors, name: c.nameValue(), requests: c.nameRequests };
});

/* ── A program already on: the modal says the aired part will be missed ─────────────────
   dev/changelog/1275. Times are relative to the real clock, since "already on" is a
   question about now; the fixed 2026-08 program above has long since ended, which is the
   "nothing to say" case for every scenario before this one. */
const isoAgo = (minutes) => new Date(Date.now() - minutes * 60000).toISOString().slice(0, 19);
const ON_NOW = { ...PROG, id: 601, start_time: isoAgo(20), stop_time: isoAgo(-40) };

await record('late_program', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  return { errors: c.errors, visible: c.lateVisible(), text: c.lateText() };
});

await record('late_program_start_typed_future', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  const before = c.lateVisible();
  c.typeStart('2099-01-01T20:00');
  return { errors: c.errors, before, visible: c.lateVisible() };
});

await record('late_program_profile_change_keeps_it', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  c.selectProfile('1');
  return { errors: c.errors, visible: c.lateVisible(), text: c.lateText() };
});

await record('future_program', async () => {
  const c = boot();
  c.openModal({ ...PROG, id: 602, start_time: isoAgo(-30), stop_time: isoAgo(-90) }, CH);
  return { errors: c.errors, visible: c.lateVisible() };
});

await record('late_then_edit_hides_it', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  const before = c.lateVisible();
  c.openModal({ ...ON_NOW, has_recording: true, recording_id: 78, recording_status: 'SCHEDULED',
                recording_start_time: isoAgo(-60), recording_stop_time: isoAgo(-120),
                recording_profile_id: null }, CH);
  return { errors: c.errors, before, visible: c.lateVisible() };
});

/* ── A program already on says "Record now", and its warning comes first ───────────────
   dev/changelog/1290: "Schedule recording" for a show already on made a tester stop and
   wonder whether it would record now or later, and on a phone the warning sat below the
   fold behind the sheet's buttons. */
await record('record_now_labels', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  const late = c.labels();
  c.typeStart('2099-01-01T20:00');
  const typedFuture = c.labels();
  c.openModal({ ...PROG, id: 603, start_time: isoAgo(-30), stop_time: isoAgo(-90) }, CH);
  const future = c.labels();
  return { errors: c.errors, late, typedFuture, future, lateText: (() => {
    c.openModal(ON_NOW, CH); return c.lateText(); })() };
});

await record('replace_keeps_its_words', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH, { replaceRecId: 55 });
  return { errors: c.errors, labels: c.labels(), lateText: c.lateText() };
});

await record('modal_layout', async () => {
  const c = boot();
  c.openModal(ON_NOW, CH);
  const body = c.$('#record-modal .modal-body');
  const order = Array.from(body.querySelectorAll('#modal-late-note, #modal-name, #modal-start'))
    .map((el) => el.id);
  const pick = c.$('#modal-url-pick');
  const inPick = pick ? Boolean(pick.querySelector('#modal-url-display')) : false;
  const openedOnce = pick ? (pick.open = true, true) : false;
  c.openModal(ON_NOW, CH);
  return { errors: c.errors, order, isDetails: pick ? pick.tagName : null, inPick, openedOnce,
           openAfterReopen: pick ? pick.open : null,
           urlValue: c.$('#modal-url').value };
});

/* ── After saving a recording that starts now, the page asks again soon ─────────────────
   dev/changelog/1290: the grid redrew before the recorder started and nothing asked again
   until the next nav poll, so the cell said SCHEDULED for 8 to 10 s. */
await record('start_followup', async () => {
  const run = async (prog) => {
    const c = boot();
    const followups = [];
    const delays = [];
    c.window.fetchNavStatus = () => followups.push('nav');
    // Any timer of a second or more runs at once, and the ones that asked for nav status
    // are the follow-ups - their delays are what the assertions read.
    const realTimeout = c.window.setTimeout;
    c.window.setTimeout = (fn, delay, ...rest) => {
      if (delay >= 1000) {
        const before = followups.length;
        fn();
        if (followups.length > before) delays.push(delay);
        return 0;
      }
      return realTimeout.call(c.window, fn, delay, ...rest);
    };
    c.openModal(prog, CH);
    c.$('#modal-form').dispatchEvent(new c.window.Event('submit', { bubbles: true, cancelable: true }));
    await settle(c.window);
    await settle(c.window);
    return { errors: c.errors, followups: followups.length, delays };
  };
  return { now: await run(ON_NOW),
           later: await run({ ...PROG, id: 604, start_time: isoAgo(-30), stop_time: isoAgo(-90) }) };
});

console.log(JSON.stringify(out));
// /guide's own setInterval(updateNowLine, ...) / setInterval(fetchAndRender, ...) keep the
// event loop alive forever otherwise - this harness never needs them to fire.
process.exit(0);
