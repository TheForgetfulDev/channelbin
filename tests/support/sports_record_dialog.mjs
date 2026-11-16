/* Drives the REAL league page with the REAL shared record dialog (util.js, guide.js, live-now.js
   and sports.js over the markup the app rendered, dev/changelog/1539) in jsdom and prints what it
   observed as JSON: Record's pick notice and the event field drawn into _record_modal.html, then
   both cleared when the dialog is opened again the way every other page opens it. Called by
   tests/test_sports_record.py, which owns every assertion. Faked: fetch (answers per request) and
   swapFromServer. jsdom computes no layout. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/league.html`, 'utf8');
const JS = ['util.js', 'guide.js', 'live-now.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const LIVE = JSON.parse(fs.readFileSync(`${DIR}/live.json`, 'utf8'));
const CTX = JSON.parse(fs.readFileSync(`${DIR}/context.json`, 'utf8'));
const EVENT = JSON.parse(fs.readFileSync(`${DIR}/event.json`, 'utf8'));
const wait = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/navigation|reload|scrollTo/i.test(e.message)) errors.push(e.message); });
let answer = LIVE;
const fetchStub = (url) => {
  const path = String(url).replace('http://localhost:5000', '');
  let payload = [];
  if (/\/api\/sports\/events\/\d+\/record$/.test(path)) payload = answer;
  else if (/\/record-context/.test(path)) payload = CTX;
  else if (/\/api\//.test(path)) payload = { success: true };
  return Promise.resolve({ ok: true, status: 200, headers: { get: () => 'application/json' },
                           json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
};
// 'dangerously' so the page's own inline scripts run (its GUIDE_CONFIG is a top-level const
// there, as in a browser); the <script src> files are not fetched, and are evaluated below in ONE
// eval, since separate evals share no top-level const/let scope in jsdom
// (tests/support/guide_modal.mjs says more).
const dom = new JSDOM(HTML, {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost:5000/sports/1', virtualConsole: vc,
  beforeParse(win) {
    win.fetch = fetchStub;
    win.matchMedia = (query) => ({ media: query, matches: false, onchange: null, addEventListener() {},
                                   removeEventListener() {}, addListener() {}, removeListener() {},
                                   dispatchEvent() { return false; } });
  },
});
const w = dom.window;
const d = w.document;
w.scrollTo = () => {};
w.eval(JS.join('\n'));
w.eval('swapFromServer = function () { return Promise.resolve(); };');
d.dispatchEvent(new w.Event('DOMContentLoaded'));
await wait();

const note = () => d.getElementById('modal-pick-note');
const state = () => ({
  open: d.getElementById('record-modal').style.display === 'flex',
  note_shown: note().style.display !== 'none',
  note_class: note().className,
  note_text: note().textContent.replace(/\s+/g, ' ').trim(),
  note_link: (note().querySelector('a') || {}).getAttribute ? note().querySelector('a').getAttribute('href') : null,
  sports_event: d.getElementById('modal-sports-event').value,
  epg_id: d.getElementById('modal-epg-id').value,
  head: d.getElementById('modal-prog-head').textContent.replace(/\s+/g, ' ').trim(),
});
const out = { errors };
const record = Array.from(d.querySelectorAll('[data-act="record"]')).filter((b) => !b.dataset.why);

record[0].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await wait();
out.live = state();

answer = EVENT;
record[0].dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
await wait();
out.event = state();

// Opened the way the TV Guide, the Recordings list and the search page open it: no options.
w.openModal({ id: 77, title: 'Some Show', start_time: '2099-01-01T20:00:00', stop_time: '2099-01-01T21:00:00',
              stream_url: 'http://example.test/live/1', channel_id: 1, channel_name: 'Local 1' }, null);
await wait();
out.plain = state();

console.log(JSON.stringify(out));
process.exit(0);
