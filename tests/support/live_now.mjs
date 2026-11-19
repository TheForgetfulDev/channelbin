/* Drives static/js/live-now.js::goLiveNow() in jsdom over the shipped util.js and prints what it
   observed as JSON. Called by tests/test_live_now_js.py, which owns every assertion - this file
   only reports. fetch is answered here: 409 for event 13, success for the rest.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const LIVE = fs.readFileSync(`${REPO}/static/js/live-now.js`, 'utf8');

const channels = JSON.stringify(['Ironvale Bears', 'Pretend League 3']).replace(/"/g, '&quot;');
const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <button id="on" data-act="live-now" data-event="7" data-on="0" data-channels="${channels}"
    data-message="Gulls at Bears is scheduled to start today at 7:00 PM.">Start broadcast</button>
  <button id="off" data-act="live-now" data-event="7" data-on="1">Stop broadcast</button>
  <button id="why" data-act="live-now" data-event="8" data-on="0" data-why="No sports channel carries it.">Start broadcast</button>
  <button id="stale" data-act="live-now" data-event="13" data-on="1">Stop broadcast</button>
</body></html>`;

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;
const calls = [];
w.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method, body: opts.body ? JSON.parse(opts.body) : null });
  const refused = url.includes('/13/');
  const body = refused ? { error: 'That game is no longer in the schedule.' }
    : { success: true, message: 'Broadcasting Gulls at Bears early, until 12:30 AM.' };
  return { ok: !refused, status: refused ? 409 : 200, statusText: '', json: async () => body };
};
w.eval(UTIL);
w.eval(LIVE);

const toast = () => {
  const t = d.getElementById('app-toast');
  return t ? { text: t.textContent, cls: t.className } : null;
};
const modal = () => d.querySelector('.modal');

async function run(id, act) {
  calls.length = 0;
  const p = w.goLiveNow(d.getElementById(id));
  await new Promise((r) => setTimeout(r, 0));
  const ov = modal();
  const shown = ov ? {
    title: ov.querySelector('.modal-head h2').textContent,
    paras: Array.from(ov.querySelectorAll('.modal-body p')).map((el) => el.textContent),
    list: Array.from(ov.querySelectorAll('.modal-body li')).map((el) => el.textContent),
    buttons: Array.from(ov.querySelectorAll('.modal-foot .btn')).map((b) => b.textContent),
  } : null;
  if (ov && act) act(Array.from(ov.querySelectorAll('.modal-foot .btn')));
  const result = await p;
  return { modal: shown, result, calls: [...calls], toast: toast() };
}

const out = { errors };
out.on = await run('on', (b) => b[1].click());
out.cancelled = await run('on', (b) => b[0].click());
out.off = await run('off');
out.why = await run('why');
out.refused = await run('stale');
process.stdout.write(JSON.stringify(out));
