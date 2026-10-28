/* Drives the REAL /guide page in jsdom and reports, as JSON, what a click on a program opened
   and what each of its actions did. Called by tests/test_program_chooser_js.py, which owns
   every assertion - this file only reports.

   Why it exists: dev/changelog/1321. A program click opens the program chooser instead of the
   record dialog, and every action in it hands off to something that already exists - the
   record dialog in each of its modes, Test now, the live preview, the recording player, the
   airing search and the channel and group pages. All of that happens in the browser.

   Real: the page markup is what the Flask /guide route rendered (so GUIDE_CONFIG, the record
   dialogs and the status vocabulary are the app's own), and util.js, guide.js,
   live-preview.js, recording-player.js and program-chooser.js are the shipped files. Faked:
   the network, which records every request and answers the EPG grid from a payload the
   scenario controls; the clipboard; and layout (tests/support/jsdom_layout.mjs).

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and meta.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const META = JSON.parse(fs.readFileSync(`${DIR}/meta.json`, 'utf8'));
const JS = ['util.js', 'guide.js', 'live-preview.js', 'recording-player.js', 'program-chooser.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const iso = (minFromNow) => new Date(Date.now() + minFromNow * 60000).toISOString().slice(0, 19);

function prog(over = {}) {
  return {
    id: 501, title: 'Evening News', sub_title: 'Tonight', description: 'The day in full.',
    start_time: iso(60), stop_time: iso(120), stream_url: 'http://example.test/live/u1/p1/9.ts',
    channel_id: META.channelId, channel_name: 'Test Channel', group_id: null,
    suggested_name: 'Evening News', has_recording: false, recording_id: null,
    recording_status: null, recording_start_time: null, recording_stop_time: null,
    matched_tags: [], is_dummy: false,
    ...over,
  };
}

function row(programs, over = {}) {
  return {
    id: META.channelId, name: 'Test Channel', account_name: 'Acct', account_color: '#3366ff',
    default_profile_id: null, programs, recordings: [], health_score: 80,
    ...over,
  };
}

const GROUP = {
  id: 'g5', name: 'ESPN', is_group: true, group_id: 5, active_channel_id: 12,
  active_channel_name: 'US: ESPN FHD', account_name: 'Acct', default_profile_id: null,
};

function boot({ phone = false } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const sent = [];
  const server = { epg: { channels: [] } };
  const fetchStub = (target, init = {}) => {
    const href = String(target);
    sent.push({ href, method: (init.method || 'GET').toUpperCase() });
    let payload = [];
    if (href.includes('/api/guide/epg')) payload = server.epg;
    else if (href.includes('/test-now')) payload = { success: true, message: 'server words' };
    else if (href.includes('/preview')) {
      payload = { status_url: '/api/preview/s1/status', stop_url: '/api/preview/s1/stop',
                  playlist_url: '/api/preview/s1/index.m3u8' };
    } else if (href.includes('/api/preview/')) payload = { state: 'STARTING', elapsed_seconds: 1 };
    else if (href.includes('/file/check')) payload = { capture: true, live: true, download: false };
    return Promise.resolve({
      ok: true, status: 200, headers: { get: () => 'application/json' },
      json: () => Promise.resolve(payload), text: () => Promise.resolve(JSON.stringify(payload)),
    });
  };

  const copied = [];
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/guide',
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = fetchStub;
      // guide.html's tail script names fetchAndRender before guide.js defines it
      // (tests/support/guide_modal.mjs explains the stand-in).
      w.fetchAndRender = function () {};
      Object.defineProperty(w, 'innerWidth', { value: phone ? 375 : 1280, configurable: true });
      w.matchMedia = (query) => ({
        media: query, matches: phone && query.includes('max-width'), onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
      Object.defineProperty(w.navigator, 'clipboard', {
        value: { writeText: (t) => { copied.push(t); return Promise.resolve(); } },
        configurable: true,
      });
    },
  });
  const { window } = dom;
  const { document } = window;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.HTMLMediaElement.prototype.play = () => Promise.resolve();
  window.HTMLMediaElement.prototype.pause = () => {};
  window.HTMLMediaElement.prototype.load = () => {};
  fakeLayout(window);
  // One eval, so every file sees the others' top-level declarations (guide_modal.mjs).
  window.eval(JS.join('\n'));
  document.dispatchEvent(new window.Event('DOMContentLoaded', { bubbles: true, cancelable: true }));

  const $ = (s) => document.querySelector(s);
  const panel = () => document.getElementById('side-panel');
  const visible = (el) => Boolean(el) && el.getClientRects().length > 0;
  const tilesIn = (root) => (root ? Array.from(root.querySelectorAll('.act-tile')).map((t) => ({
    label: t.querySelector('.act-tile-label').textContent,
    act: t.dataset.act || null,
    href: t.getAttribute('href'),
    tag: t.tagName.toLowerCase(),
    disabled: Boolean(t.disabled),
    icon: (t.querySelector('svg.icon') || { dataset: {} }).dataset.icon || null,
  })) : []);
  const openDialogs = () => Array.from(document.querySelectorAll('.modal')).filter(visible);
  const dialogs = () => openDialogs().map((m) => (m.querySelector('h2') || {}).textContent);
  // Where the chooser is: the side panel, or the phone's sheet (the dialog on top).
  const root = () => (visible(panel()) ? panel() : openDialogs().pop() || null);

  const c = {
    window, document, errors, sent, server, copied, $,
    settle: (ms = 40) => new Promise((r) => setTimeout(r, ms)),
    open: (p, ch) => window.openProgramChooser(p, ch),
    panelOpen: () => visible(panel()),
    panelTitle: () => (panel() ? panel().querySelector('h2').textContent : null),
    panelCount: () => document.querySelectorAll('#side-panel').length,
    tiles: () => tilesIn(root()),
    note: () => {
      const n = root() && root().querySelector('.act-tiles-note');
      return n ? n.textContent : null;
    },
    servingNote: () => {
      const n = root() && root().querySelector('.chooser-note');
      return n ? n.textContent : null;
    },
    progHead: () => {
      const h = root() && root().querySelector('.prog-head');
      return h ? { text: h.textContent, hasTitleHeading: Boolean(h.querySelector('.prog-head-title')) } : null;
    },
    click: (label) => {
      const t = Array.from(root().querySelectorAll('.act-tile'))
        .find((x) => x.querySelector('.act-tile-label').textContent === label);
      t.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }));
    },
    escape: () => document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })),
    recordModal: () => ({ open: visible($('#record-modal')), title: $('#modal-title').textContent.trim() }),
    activeModalOpen: () => visible($('#active-rec-modal')),
    dialogs,
    toast: () => (($('#app-toast') || {}).textContent || ''),
    posts: () => sent.filter((s) => s.method === 'POST').map((s) => s.href),
  };
  return c;
}

const out = {};
const record = async (name, fn) => {
  try {
    out[name] = await fn();
  } catch (e) {
    out[name] = { error: `${e.message}\n${e.stack}` };
  }
};

// A real cell click: the grid is drawn from the EPG payload, then its cell is clicked.
await record('cell_click', async () => {
  const c = boot();
  const start = iso(10);
  const stop = iso(70);
  c.server.epg = { channels: [row([prog({ start_time: start, stop_time: stop })])] };
  c.window.fetchAndRender();
  await c.settle();
  const cell = c.document.querySelector('.guide-program');
  if (!cell) return { errors: c.errors, cell: false };
  cell.dispatchEvent(new c.window.MouseEvent('click', { bubbles: true, button: 0 }));
  await c.settle();
  const opened = { panelOpen: c.panelOpen(), title: c.panelTitle(), tiles: c.tiles(),
                   recordModal: c.recordModal(), progHead: c.progHead() };
  // The grid re-reads its data under the open panel: the program is now scheduled.
  c.server.epg = { channels: [row([prog({
    start_time: start, stop_time: stop, has_recording: true, recording_id: 77,
    recording_status: 'SCHEDULED', recording_start_time: start, recording_stop_time: stop,
  })])] };
  c.window.fetchAndRender();
  await c.settle();
  return { errors: c.errors, cell: true, opened,
           afterRefresh: { panelOpen: c.panelOpen(), tiles: c.tiles() } };
});

await record('upcoming', async () => {
  const c = boot();
  c.open(prog(), row([]));
  await c.settle();
  const opened = { panelOpen: c.panelOpen(), title: c.panelTitle(), tiles: c.tiles(),
                   note: c.note(), recordModal: c.recordModal(), dialogs: c.dialogs() };
  c.click('Schedule recording');
  await c.settle();
  return { errors: c.errors, opened,
           afterRecord: { panelOpen: c.panelOpen(), recordModal: c.recordModal() } };
});

await record('scheduled', async () => {
  const c = boot();
  c.open(prog({ has_recording: true, recording_id: 77, recording_status: 'SCHEDULED',
                recording_start_time: iso(60), recording_stop_time: iso(120) }), row([]));
  await c.settle();
  const tiles = c.tiles();
  c.click('Edit recording');
  await c.settle();
  return { errors: c.errors, tiles, afterEdit: { panelOpen: c.panelOpen(), recordModal: c.recordModal() } };
});

await record('capturing', async () => {
  const c = boot();
  const p = prog({ start_time: iso(-20), stop_time: iso(40), has_recording: true,
                   recording_id: 78, recording_status: 'IN_PROGRESS',
                   recording_start_time: iso(-20), recording_stop_time: iso(40) });
  c.open(p, row([]));
  await c.settle();
  const tiles = c.tiles();
  c.click('Recording controls');
  await c.settle();
  const controls = { panelOpen: c.panelOpen(), activeOpen: c.activeModalOpen(),
                     recordModalOpen: c.recordModal().open };
  c.window.closeActiveRecModal();
  c.open(p, row([]));
  await c.settle();
  c.click('Play');
  await c.settle();
  return { errors: c.errors, tiles, controls,
           afterPlay: { panelOpen: c.panelOpen(), dialogs: c.dialogs(),
                        gets: c.sent.map((s) => s.href).filter((h) => h.includes('/file/check')) } };
});

await record('ended', async () => {
  const c = boot();
  c.open(prog({ start_time: iso(-90), stop_time: iso(-30) }), row([]));
  await c.settle();
  const tiles = c.tiles();
  const note = c.note();
  c.click('Schedule recording');
  await c.settle();
  return { errors: c.errors, tiles, note,
           afterClick: { panelOpen: c.panelOpen(), recordModal: c.recordModal() } };
});

await record('dummy', async () => {
  const c = boot();
  c.open(prog({ id: null, is_dummy: true, title: 'Test Channel', start_time: iso(-90),
                stop_time: iso(-30) }), row([]));
  await c.settle();
  return { errors: c.errors, tiles: c.tiles(), note: c.note(), progHead: c.progHead() };
});

await record('group', async () => {
  const c = boot();
  const p = prog({ start_time: iso(-10), stop_time: iso(50), channel_id: 12, group_id: 5,
                   channel_name: 'ESPN' });
  c.open(p, GROUP);
  await c.settle();
  const tiles = c.tiles();
  const servingNote = c.servingNote();
  c.click('Test now');
  await c.settle();
  const afterTest = { panelOpen: c.panelOpen(), posts: c.posts(), toast: c.toast() };
  c.click('Copy stream URL');
  await c.settle();
  const afterCopy = { copied: c.copied.slice(), toast: c.toast(), panelOpen: c.panelOpen() };
  c.click('Preview');
  await c.settle();
  return { errors: c.errors, tiles, servingNote, afterTest, afterCopy,
           afterPreview: { panelOpen: c.panelOpen(), posts: c.posts(), dialogs: c.dialogs() } };
});

await record('refill_and_escape', async () => {
  const c = boot();
  c.open(prog(), row([]));
  c.open(prog({ id: 502, title: 'Late Show' }), row([]));
  await c.settle();
  const refilled = { count: c.panelCount(), title: c.panelTitle() };
  // A dialog over the panel takes the first Escape; the second closes the panel.
  c.window.buildModal({ title: 'Something on top', body: 'x' });
  c.escape();
  await c.settle();
  const afterFirst = { panelOpen: c.panelOpen(), dialogs: c.dialogs() };
  c.escape();
  await c.settle();
  return { errors: c.errors, refilled, afterFirst, afterSecond: { panelOpen: c.panelOpen() } };
});

await record('single_channel', async () => {
  const c = boot();
  c.window.eval('GUIDE_CONFIG.singleChannel = true');
  c.open(prog(), row([]));
  await c.settle();
  return { errors: c.errors, tiles: c.tiles() };
});

await record('phone', async () => {
  const c = boot({ phone: true });
  c.open(prog({ start_time: iso(-10), stop_time: iso(50) }), row([]));
  await c.settle();
  const opened = { panelOpen: c.panelOpen(), dialogs: c.dialogs(), tiles: c.tiles(),
                   progHead: c.progHead() };
  c.click('Record now');
  await c.settle();
  return { errors: c.errors, opened,
           afterRecord: { dialogs: c.dialogs(), recordModal: c.recordModal() } };
});

console.log(JSON.stringify(out));
// /guide's own intervals keep the event loop alive otherwise.
process.exit(0);
