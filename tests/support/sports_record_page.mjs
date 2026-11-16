/* Drives the REAL league page's Record and Manage recording (static/js/sports.js over the markup the
   app rendered, mockup 54 round 2, dev/changelog/1539) in jsdom and prints what it observed as JSON.
   Called by tests/test_sports_record.py, which owns every assertion - this file only reports.
   Faked: fetch (answers per request), swapFromServer, showToast, and guide.js's openModal, which
   this records rather than draws: the shared record dialog has its own tests. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/league.html`, 'utf8');
const JS = ['util.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/navigation|reload/i.test(e.message)) errors.push(e.message); });
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:5000/sports/1', virtualConsole: vc });
const w = dom.window;
const d = w.document;
const requests = [];
const PICK = { success: true, kind: 'live', epg_id: 55, group_id: 9,
               notice: { tone: 'info', strong: 'ChannelBin picked this listing.', text: 'It is the live airing.' },
               search_url: '/channels?grain=airings&f.event=7001' };
const CTX = { success: true, program: { id: 55, title: 'PFL Football' }, channel: { id: 3, default_profile_id: null } };
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  requests.push({ method, path });
  let payload = { success: true };
  if (method === 'GET' && /\/record$/.test(path)) payload = PICK;
  if (method === 'GET' && /\/record-context/.test(path)) payload = CTX;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
};
w.eval(JS[0]);
w.eval(`window.SWAPS = 0; swapFromServer = function () { SWAPS += 1; return Promise.resolve(); };
  window.TOASTS = []; showToast = function (m) { TOASTS.push(m); };
  window.OPENED = []; window.openModal = function (prog, ch, opts) { OPENED.push({ prog, ch, opts: opts || {} }); };`);
w.eval(JS[1]);

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(30); };
const out = { errors };
const confirmBtn = (label) => Array.from(d.querySelectorAll('.modal:not([id]) .modal-foot button'))
  .find((b) => b.textContent.trim() === label);

// A greyed Record says why and asks nothing.
const greyed = d.querySelector('[data-act="record"][aria-disabled="true"]');
let n = requests.length;
await click(greyed);
out.greyed_toast = w.TOASTS.slice(-1)[0] || null;
out.greyed_requests = requests.length - n;

// Record: the pick, then the shared record-context for its listing and group, then the dialog.
n = requests.length;
await click(d.querySelector('[data-act="record"]:not([aria-disabled])'));
out.record_requests = requests.slice(n).map((r) => `${r.method} ${r.path}`);
out.opened = w.OPENED.slice(-1)[0] || null;

// Search programs' greyed entry says why.
const why = d.querySelector('[data-act="search-why"]');
if (why) { await click(why); out.search_toast = w.TOASTS.slice(-1)[0]; }

// Edit recording opens the same dialog in edit mode.
await click(d.querySelector('[data-act="edit-rec"]'));
out.edit = w.OPENED.slice(-1)[0] || null;

// Cancel recording: the Recordings list's confirm, then its route, then a swap.
let swaps = w.SWAPS;
await click(d.querySelector('[data-act="cancel-rec"]'));
out.cancel_title = (d.querySelector('.modal:not([id]) h2') || {}).textContent || null;
out.cancel_body = (d.querySelector('.modal:not([id]) .modal-body') || {}).textContent || null;
n = requests.length;
await click(confirmBtn('Cancel recording'));
out.cancel_requests = requests.slice(n).map((r) => `${r.method} ${r.path}`);
out.cancel_swaps = w.SWAPS - swaps;

// Stop recording, the same way.
swaps = w.SWAPS;
await click(d.querySelector('[data-act="stop-rec"]'));
out.stop_title = (d.querySelector('.modal:not([id]) h2') || {}).textContent || null;
n = requests.length;
await click(confirmBtn('Stop recording'));
out.stop_requests = requests.slice(n).map((r) => `${r.method} ${r.path}`);
out.stop_swaps = w.SWAPS - swaps;

// Saving from the record dialog swaps the page.
swaps = w.SWAPS;
w.onScheduleSaved();
await wait(30);
out.saved_swaps = w.SWAPS - swaps;

console.log(JSON.stringify(out));
process.exit(0);
