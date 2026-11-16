/* Drives the REAL Sports Schedules league page's pagers and team filter (static/js/sports.js over the
   markup the app rendered) in jsdom and prints what it observed as JSON (mockup 57). Called by
   tests/test_sports_league_paging.py, which owns every assertion - this file only reports. Faked:
   fetch and swapFromServer, which records the regions it was asked for and the address it would read. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/league.html`, 'utf8');
const URL0 = fs.readFileSync(`${DIR}/url.txt`, 'utf8').trim();
const JS = ['util.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/navigation|reload/i.test(e.message)) errors.push(e.message); });
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: `http://localhost:5000${URL0}`, virtualConsole: vc });
const w = dom.window;
const d = w.document;
w.scrollTo = () => {};
w.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ success: true }),
                                  text: () => Promise.resolve(HTML) });
w.eval(JS[0]);
// An address with its query in name order, so a test compares what it says, not how it is spelled.
w.eval(`window.SORTED = function (href) {
  const u = new URL(href, location.href);
  u.searchParams.sort();
  return u.pathname + u.search;
};
window.SWAPS = [];
swapFromServer = function (selectors, url) {
  SWAPS.push({ selectors, url: SORTED(url || location.href) });
  return Promise.resolve();
};`);
w.eval(JS[1]);

const out = { errors };
const here = () => w.SORTED(w.location.href);
const click = async (el, init = {}) => {
  const ev = new w.MouseEvent('click', { bubbles: true, cancelable: true, ...init });
  el.dispatchEvent(ev);
  await wait();
  return ev.defaultPrevented;
};
const swapsSince = (n) => w.SWAPS.slice(n);

let n = w.SWAPS.length;
out.games_next_prevented = await click(d.querySelector('#sp-upcoming a.sp-next'));
out.games_next = { url: here(), swaps: swapsSince(n) };

n = w.SWAPS.length;
out.feed_next_prevented = await click(d.querySelector('#sp-feed-list a.sp-next'));
out.feed_next = { url: here(), swaps: swapsSince(n) };

n = w.SWAPS.length;
out.ctrl_click_prevented = await click(d.querySelector('#sp-feed-list a.sp-next'), { ctrlKey: true });
out.ctrl_click_swaps = swapsSince(n).length;

const input = d.getElementById('sp-feed-q');
n = w.SWAPS.length;
for (const value of ['o', 'oh', 'ohi']) {
  input.value = value;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(40);
}
out.typing_before_pause = swapsSince(n).length;
await wait(400);
out.filtered = { url: here(), swaps: swapsSince(n), has_text: d.getElementById('sp-feed-wrap').classList.contains('has-text'),
                 default_matches: input.defaultValue === input.value };

n = w.SWAPS.length;
await click(d.getElementById('sp-feed-clear'));
await wait(30);
out.cleared = { url: here(), swaps: swapsSince(n), value: input.value,
                has_text: d.getElementById('sp-feed-wrap').classList.contains('has-text') };

console.log(JSON.stringify(out));
// The page's live refresh keeps a timer running, so node would otherwise wait on it forever.
process.exit(0);
