/* Drives the REAL /guide page in jsdom and prints what it observed as JSON.
   Called by tests/test_record_modal_head_js.py, which owns every assertion - this file
   only reports, so a failure reads as "the modal did X" in Python rather than as a node
   exit code. Same shape as tests/support/guide_modal.mjs beside it.

   Why it exists: dev/changelog/1050 moved the program's own details into the record modal
   (#modal-prog-head) and deleted the mobile program sheet that used to carry them. What the
   header shows, and for which kinds of target it shows at all, is entirely client-side DOM
   behaviour that Python cannot reach - openModal() is handed a plain object and writes
   innerHTML.

   What is real here and what is not: the markup is what the Flask /guide route really
   rendered, and util.js + guide.js are the shipped files, evaluated into the window the way
   their <script src> would have. Faked: the network (savedSearches and the EPG grid fetch
   are both stubbed to empty responses, and the group record-context lookup answers a fixed
   payload) - the modal is driven directly via window.openModal(prog, ch) with a synthetic
   program object, the same shape the real EPG API returns, rather than through a live grid
   cell click. jsdom computes no layout, so this says nothing about how the header LOOKS -
   only about what text and elements it contains.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');

const JS = ['util.js', 'guide.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

// The group disclosure's lookup (/api/channel-groups/<id>/record-context). Named members
// so the Python side can assert the sentence really quotes the serving member.
const GROUP_CTX = {
  group: { id: 4, name: 'Movie Channels' },
  serving: { id: 77, name: 'Cinema One FHD', account_name: 'Provider A' },
  format_override: false,
  locked_format: null,
};

function boot({ mobile = false } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const fetchStub = (target) => {
    const href = String(target);
    let payload = [];
    if (href.includes('/api/guide/epg')) payload = { channels: [] };
    if (href.includes('/record-context')) payload = GROUP_CTX;
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
      // guide.html's tail inline script references window.fetchAndRender before guide.js
      // (a <script src>, which jsdom never fetches) has defined it; guide.js's own
      // declaration overwrites this stub once evaluated.
      w.fetchAndRender = function () {};
      Object.defineProperty(w, 'innerWidth', { value: mobile ? 390 : 1280, configurable: true });
      // isMobileGuide() reads this once per call through guide.js's MOBILE_MQ, so a
      // `matches: true` boot really is the phone-width code path.
      w.matchMedia = (query) => ({
        media: query, matches: mobile, onchange: null,
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
  // window.eval() calls do not share a top-level lexical scope in jsdom the way separate
  // <script> tags do in a real browser.
  window.eval(JS.join('\n'));
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const $ = (s) => document.querySelector(s);
  const head = () => $('#modal-prog-head');

  return {
    window, document, errors, $,
    openModal: (prog, ch) => window.openModal(prog, ch),
    isMobile: () => window.isMobileGuide(),
    headVisible: () => head().style.display !== 'none',
    headHtml: () => head().innerHTML,
    headText: () => head().textContent.replace(/\s+/g, ' ').trim(),
    headLabels: () => [...head().querySelectorAll('.info-lbl')].map((e) => e.textContent),
    headValues: () => [...head().querySelectorAll('.info-val')].map((e) => e.textContent),
    headTitle: () => (head().querySelector('.prog-head-title') || {}).textContent || '',
    headSub: () => (head().querySelector('.info-sub') || {}).textContent || '',
    headDesc: () => (head().querySelector('.info-desc') || {}).textContent || '',
    headTags: () => [...head().querySelectorAll('.info-tag')].map((e) => e.textContent.trim()),
    modalOpen: () => $('#record-modal').style.display !== 'none',
    nameValue: () => $('#modal-name').value,
    groupNoteText: () => (($('#modal-group-note') || {}).textContent || '').replace(/\s+/g, ' ').trim(),
    groupNoteVisible: () => $('#modal-group-note').style.display !== 'none',
    // The thing the header replaced. Nothing may reintroduce it.
    nameHintExists: () => !!$('#modal-name-hint'),
    sheetPanels: () => document.querySelectorAll('.guide-sheet').length,
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

// Deliberately longer than the 120 characters the old #modal-name-hint sliced it to, so a
// silent reintroduction of that truncation is visible in the assertion rather than only in
// the code.
const LONG_DESC =
  'A documentary crew follows three lighthouse keepers through a winter on the north '
  + 'Atlantic coast, from the first storm of the season to the day the supply boat finally '
  + 'reaches them again.';

// Far from "now" so nothing in openModal's now-line or scroll logic can interfere.
const PROG = {
  id: 501,
  title: 'The Long Watch',
  sub_title: 'Part Two: The Supply Boat',
  description: LONG_DESC,
  suggested_name: 'The Long Watch - Part Two',
  start_time: '2026-08-10T20:00:00',
  stop_time: '2026-08-10T21:30:00',
  channel_name: 'Documentary HD',
  stream_url: 'http://example.test/live/1',
  channel_id: 9,
  group_id: null,
  has_recording: false,
  is_dummy: false,
  matched_tags: [{ name: 'Documentary', color: '#3b82f6' }, { name: 'Nature', color: '#22c55e' }],
};
const CH = { id: 9, name: 'Documentary HD', default_profile_id: null };

/* ── A real EPG program: the whole header, including the WHOLE description ────────── */
await record('full_program', async () => {
  const c = boot();
  c.openModal(PROG, CH);
  return {
    errors: c.errors,
    modalOpen: c.modalOpen(),
    headVisible: c.headVisible(),
    title: c.headTitle(),
    sub: c.headSub(),
    desc: c.headDesc(),
    labels: c.headLabels(),
    values: c.headValues(),
    tags: c.headTags(),
    html: c.headHtml(),
    nameHintExists: c.nameHintExists(),
  };
});

/* ── A program with no subtitle, description or tags: lines only, no empty elements ── */
await record('bare_program', async () => {
  const c = boot();
  c.openModal({ ...PROG, sub_title: '', description: '', matched_tags: [] }, CH);
  return {
    errors: c.errors,
    headVisible: c.headVisible(),
    title: c.headTitle(),
    sub: c.headSub(),
    desc: c.headDesc(),
    tags: c.headTags(),
    labels: c.headLabels(),
  };
});

/* ── The dashboard / recording-detail "edit a scheduled recording" shape ──────────── */
await record('no_program', async () => {
  const c = boot();
  c.openModal({
    has_recording: true,
    recording_id: 12,
    recording_status: 'SCHEDULED',
    recording_start_time: '2026-08-10T20:00:00',
    recording_stop_time: '2026-08-10T21:00:00',
    recording_profile_id: null,
    suggested_name: 'Saved Recording',
    title: 'Saved Recording',
    stream_url: 'http://example.test/live/1',
    channel_id: 9,
    group_id: null,
    id: null,
    description: null,
  }, null);
  return {
    errors: c.errors,
    modalOpen: c.modalOpen(),
    headVisible: c.headVisible(),
    html: c.headHtml(),
    nameValue: c.nameValue(),
  };
});

/* ── A dummy filler slot on a channel with no EPG data ───────────────────────────── */
await record('dummy_slot', async () => {
  const c = boot();
  c.openModal({
    ...PROG, id: null, is_dummy: true, title: 'Documentary HD',
    sub_title: '', description: '', matched_tags: [],
  }, CH);
  return { errors: c.errors, modalOpen: c.modalOpen(), headVisible: c.headVisible() };
});

/* ── Phone width: the record modal IS the program surface, and no sheet is built ──── */
await record('mobile_program', async () => {
  const c = boot({ mobile: true });
  c.openModal(PROG, CH);
  return {
    errors: c.errors,
    isMobile: c.isMobile(),
    modalOpen: c.modalOpen(),
    headVisible: c.headVisible(),
    desc: c.headDesc(),
    sheetPanels: c.sheetPanels(),
  };
});

/* ── The shortened channel-group disclosure ──────────────────────────────────────── */
await record('group_note', async () => {
  const c = boot();
  c.openModal({ ...PROG, group_id: 4 }, CH);
  // showGroupNote awaits a stubbed fetch; let its microtasks drain.
  await new Promise((r) => setTimeout(r, 0));
  return {
    errors: c.errors,
    visible: c.groupNoteVisible(),
    text: c.groupNoteText(),
  };
});

console.log(JSON.stringify(out));
// /guide's own setInterval(updateNowLine, ...) / setInterval(fetchAndRender, ...) keep the
// event loop alive forever otherwise - this harness never needs them to fire.
process.exit(0);
