/* Drives static/js/examples.js in jsdom and prints what it observed as JSON. Called by
   tests/test_examples_js.py, which owns every assertion - this file only reports. util.js
   and examples.js are evaluated as base.html and the page's <script src> would; only fetch
   is replaced, by a recorder that answers from the scenario's canned replies.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');
const EXAMPLES = fs.readFileSync(`${REPO}/static/js/examples.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <button type="button" data-examples-load>Try it with example channels</button>
  <button type="button" data-examples-remove>Delete the examples</button>
  <button type="button" data-examples-keep>Keep the examples</button>
</body></html>`;

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

async function scenario(replies, act) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    // A page navigation is the end of every successful path, and jsdom does not navigate.
    if (!/navigation/i.test(e.message)) errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:5000/get-started', virtualConsole: vc,
  });
  const w = dom.window;
  const d = w.document;
  const calls = [];
  w.fetch = async (url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase();
    calls.push(`${method} ${url}`);
    const [status, body] = replies[`${method} ${url}`] || [404, { error: 'no reply' }];
    return { ok: status < 400, status, statusText: String(status), json: async () => body };
  };
  w.eval(UTIL);
  w.eval(EXAMPLES);
  const modal = () => {
    const ov = d.querySelector('.modal');
    if (!ov) return null;
    return {
      title: ov.querySelector('.modal-head h2').textContent,
      items: Array.from(ov.querySelectorAll('.modal-body li')).map((el) => el.textContent),
      buttons: Array.from(ov.querySelectorAll('.modal-foot .btn')).map((b) => b.textContent),
      foot: () => Array.from(ov.querySelectorAll('.modal-foot .btn')),
    };
  };
  const shown = await act(d, modal);
  await tick(20);
  const toast = d.getElementById('app-toast');
  // What showToastAfterLoad left for the page the success path navigates to.
  const pending = Object.keys(w.sessionStorage).map((k) => w.sessionStorage.getItem(k));
  return { errors, calls, shown, toast: toast ? toast.textContent : null, pending };
}

const PLAN = { success: true, lines: ['The account "Example channels (M3UPT)"', 'The group'], blockers: [] };
const out = {};

out.removeConfirmed = await scenario({
  'GET /api/examples/removal': [200, PLAN],
  'POST /api/examples/remove': [200, { success: true, message: 'Removed.', next_url: '/get-started' }],
}, async (d, modal) => {
  d.querySelector('[data-examples-remove]').click();
  await tick(20);
  const m = modal();
  const seen = m && { title: m.title, items: m.items, buttons: m.buttons };
  m.foot()[m.foot().length - 1].click();
  return seen;
});

out.removeBacked = await scenario({
  'GET /api/examples/removal': [200, PLAN],
}, async (d, modal) => {
  d.querySelector('[data-examples-remove]').click();
  await tick(20);
  modal().foot()[0].click();
  return null;
});

out.removeBlocked = await scenario({
  'GET /api/examples/removal': [200, { success: true, lines: PLAN.lines,
                                       blockers: ['"bbc_tonight" is scheduled.'] }],
}, async (d, modal) => {
  d.querySelector('[data-examples-remove]').click();
  await tick(20);
  const m = modal();
  return m && { title: m.title, items: m.items, buttons: m.buttons };
});

out.removeRefused = await scenario({
  'GET /api/examples/removal': [200, PLAN],
  'POST /api/examples/remove': [409, { error: 'The examples were not removed. Still syncing.' }],
}, async (d, modal) => {
  d.querySelector('[data-examples-remove]').click();
  await tick(20);
  const m = modal();
  m.foot()[m.foot().length - 1].click();
  return null;
});

out.loadConfirmed = await scenario({
  'POST /api/examples': [200, { success: true, message: 'Loading.' }],
}, async (d, modal) => {
  d.querySelector('[data-examples-load]').click();
  await tick(20);
  const m = modal();
  const seen = m && { title: m.title, buttons: m.buttons };
  m.foot()[m.foot().length - 1].click();
  return seen;
});

out.keepConfirmed = await scenario({
  'POST /api/examples/keep': [200, { success: true, message: 'Yours now.' }],
}, async (d, modal) => {
  d.querySelector('[data-examples-keep]').click();
  await tick(20);
  const m = modal();
  const seen = m && { title: m.title, buttons: m.buttons };
  m.foot()[m.foot().length - 1].click();
  return seen;
});

out.keepBacked = await scenario({}, async (d, modal) => {
  d.querySelector('[data-examples-keep]').click();
  await tick(20);
  modal().foot()[0].click();
  return null;
});

// The Accounts list and the account page's sheet call the global with no button at all.
out.removeFromMenu = await scenario({
  'GET /api/examples/removal': [200, PLAN],
  'POST /api/examples/remove': [200, { success: true, message: 'Removed.', next_url: '/get-started' }],
}, async (d, modal) => {
  d.defaultView.removeExamples();
  await tick(20);
  const m = modal();
  const seen = m && { title: m.title };
  m.foot()[m.foot().length - 1].click();
  return seen;
});

console.log(JSON.stringify(out));
