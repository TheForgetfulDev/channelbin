/* Drives the REAL /guide page in jsdom and prints what the record and active-recording
   dialogs did when opened and closed every way a person can, as JSON. Called by
   tests/test_record_modal_anatomy_js.py, which owns every assertion.

   Why it exists: the two dialogs in templates/_record_modal.html moved onto the shared
   modal anatomy, util.js::wireModal (dev/changelog/1608). Before that they wired their own
   close button, backdrop and scroll lock by id, and guide.js kept one document-level Escape
   listener that ran both closes on every Escape pressed anywhere on the page, open dialog
   or not.

   Real: the markup the Flask /guide route rendered, and the shipped util.js + guide.js.
   Faked: the network (every fetch answers an empty list, the grid fetch an empty grid) and
   layout (tests/support/jsdom_layout.mjs, so syncScrollLock can tell a hidden dialog from
   an open one). argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const fetchStub = (target) => {
    const payload = String(target).includes('/api/guide/epg') ? { channels: [] } : [];
    return Promise.resolve({
      ok: true, status: 200,
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
      // guide.html's inline tail script calls this before guide.js has defined it.
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
  fakeLayout(window);
  // One eval: guide.js reads util.js's top-level consts at eval time (see
  // record_modal_head.mjs for why separate window.eval calls do not share them).
  window.eval(JS.join('\n'));
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const $ = (s) => document.querySelector(s);
  const shown = (id) => document.getElementById(id).style.display !== 'none';
  return {
    window, document, errors, $,
    recordOpen: () => shown('record-modal'),
    activeOpen: () => shown('active-rec-modal'),
    locked: () => document.body.classList.contains('scroll-locked'),
    escape: () => document.dispatchEvent(
      new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })),
    flush: () => new Promise((r) => setTimeout(r, 0)),
  };
}

const PROG = {
  id: 501,
  title: 'The Long Watch',
  sub_title: '',
  description: '',
  suggested_name: 'The Long Watch',
  start_time: '2030-08-10T20:00:00',
  stop_time: '2030-08-10T21:30:00',
  channel_name: 'Documentary HD',
  stream_url: 'http://example.test/live/1',
  channel_id: 9,
  group_id: null,
  has_recording: false,
  is_dummy: false,
  matched_tags: [],
};
const CH = { id: 9, name: 'Documentary HD', default_profile_id: null };
const ACTIVE = {
  ...PROG,
  has_recording: true,
  recording_id: 33,
  recording_status: 'IN_PROGRESS',
  recording_stop_time: '2030-08-10T21:30:00',
};

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

// Every way to close each dialog: what it looked like open, and after.
const CLOSERS = {
  record: {
    x: (c) => c.$('#record-modal .modal-head .modal-close').click(),
    backdrop: (c) => c.$('#record-modal .modal-backdrop').click(),
    escape: (c) => c.escape(),
    cancel: (c) => c.$('#modal-cancel').click(),
  },
  active: {
    x: (c) => c.$('#active-rec-modal .modal-head .modal-close').click(),
    backdrop: (c) => c.$('#active-rec-modal .modal-backdrop').click(),
    escape: (c) => c.escape(),
    close: (c) => c.$('#active-rec-close').click(),
  },
};

for (const [which, closers] of Object.entries(CLOSERS)) {
  for (const [how, closeIt] of Object.entries(closers)) {
    await record(`${which}_${how}`, async () => {
      const c = boot();
      const isOpen = which === 'record' ? c.recordOpen : c.activeOpen;
      const before = { open: isOpen(), locked: c.locked() };
      c.window.openModal(which === 'record' ? PROG : ACTIVE, CH);
      await c.flush();
      const opened = { open: isOpen(), locked: c.locked() };
      closeIt(c);
      await c.flush();
      return {
        errors: c.errors, before, opened,
        closed: { open: isOpen(), locked: c.locked() },
        otherOpen: which === 'record' ? c.activeOpen() : c.recordOpen(),
      };
    });
  }
}

// Escape belongs to a dialog only while it is open. The old page-wide listener ran the
// record dialog's close on every Escape, so an error shown after the dialog closed was
// hidden by the next Escape pressed anywhere.
await record('escape_after_close', async () => {
  const c = boot();
  c.window.openModal(PROG, CH);
  c.$('#modal-cancel').click();
  const err = c.$('#modal-error');
  err.style.display = 'block';
  c.escape();
  return { errors: c.errors, errorStillShown: err.style.display !== 'none' };
});

// A host's escapeHandler steps back instead of closing, as on every buildModal() dialog.
await record('escape_handler', async () => {
  const c = boot();
  c.window.openModal(PROG, CH);
  let stepped = 0;
  c.$('#record-modal').escapeHandler = () => { stepped += 1; };
  c.escape();
  return { errors: c.errors, stepped, stillOpen: c.recordOpen() };
});

// A confirm over the active-recording dialog takes Escape for itself only.
await record('confirm_over_active', async () => {
  const c = boot();
  c.window.openModal(ACTIVE, CH);
  await c.flush();
  const answer = c.window.confirmModal({ title: 'Abort?', message: 'x', confirmLabel: 'Abort' });
  c.escape();
  const confirmed = await answer;
  return {
    errors: c.errors, confirmed,
    confirmGone: !c.document.querySelector('.modal:not([id])'),
    activeStillOpen: c.activeOpen(),
    locked: c.locked(),
  };
});

// Opened twice and closed once with the X: no Escape listener is left behind, so a later
// Escape does not run the close (and its error-box reset) again.
await record('open_twice', async () => {
  const c = boot();
  c.window.openModal(PROG, CH);
  c.window.openModal(PROG, CH);
  c.$('#record-modal .modal-head .modal-close').click();
  const err = c.$('#modal-error');
  err.style.display = 'block';
  c.escape();
  return {
    errors: c.errors, open: c.recordOpen(), locked: c.locked(),
    errorStillShown: err.style.display !== 'none',
  };
});

// A step handler set during one opening does not outlive it.
await record('escape_handler_reset', async () => {
  const c = boot();
  c.window.openModal(PROG, CH);
  c.$('#record-modal').escapeHandler = () => {};
  c.$('#modal-cancel').click();
  c.window.openModal(PROG, CH);
  c.escape();
  return { errors: c.errors, open: c.recordOpen() };
});

console.log(JSON.stringify(out));
process.exit(0);
