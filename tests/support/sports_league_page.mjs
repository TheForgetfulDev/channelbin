/* Drives the REAL Sports Schedules league page (static/js/sports.js over the markup the app rendered)
   in jsdom and prints what it observed as JSON: the listings dialog's closed groups, event channels and Wrong match,
   and the Matching card's names and categories (mockup 51). Called by
   tests/test_sports_league_matches.py, which owns every assertion - this file only reports. Faked:
   fetch (answers per request) and swapFromServer. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/league.html`, 'utf8');
const LISTINGS = JSON.parse(fs.readFileSync(`${DIR}/listings.json`, 'utf8'));
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
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
  let payload = { success: true };
  if (method === 'GET' && /\/listings$/.test(path)) payload = LISTINGS;
  if (method === 'GET' && /\/categories\/offer$/.test(path)) {
    payload = { success: true, categories: [{ name: 'PFL Sunday Ticket', channels: 12, tied: false },
                                            { name: 'PFL Package', channels: 3, tied: true }] };
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
};
w.eval(JS[0]);
w.eval('window.SWAPS = 0; swapFromServer = function () { SWAPS += 1; return Promise.resolve(); };');
w.eval(JS[1]);

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(); };
const out = { errors };
const groups = () => Array.from(d.querySelectorAll('.modal .sp-group'));
const sent = (fn) => requests.filter(fn).map((r) => ({ method: r.method, path: r.path, body: r.body }));

await click(d.querySelector('.sp-onguide'));
await wait(30);
out.dialog_title = d.querySelector('.modal h2').textContent;
out.groups = groups().length;
out.closed = groups().every((g) => !g.classList.contains('open')) && d.querySelectorAll('.modal .sp-group .sp-chan').length === 0;
out.group_counts = groups().map((g) => g.querySelector('.sp-why strong').textContent);
out.group_buttons = groups().map((g) => {
  const b = g.querySelector('[data-wrong="1"]');
  return b ? { text: b.textContent.trim(), danger: b.classList.contains('btn-danger') } : null;
});
out.intro = d.querySelector('.modal .sp-dialog-body').textContent.replace(/\s+/g, ' ');

await click(groups()[0].querySelector('[data-toggle]'));
out.opened_channels = groups()[0].querySelectorAll('.sp-chan').length;
out.opened_expanded = groups()[0].querySelector('[data-toggle]').getAttribute('aria-expanded');
out.channel_buttons = Array.from(groups()[0].querySelectorAll('.sp-chan button')).map((b) => ({
  text: b.textContent.trim(), plain: b.className === 'btn btn-sm' }));
await click(groups()[0].querySelector('.sp-chan [data-wrong="1"]'));
await wait(30);
await click(groups()[0].querySelector('.sp-gact [data-wrong="1"]'));
await wait(30);
out.wrong_posts = sent((r) => r.method === 'POST' && /\/wrong$/.test(r.path));
out.still_open_after_reload = groups()[0].classList.contains('open');
const enames = () => Array.from(d.querySelectorAll('.modal .sp-ename'));
out.names = enames().map((r) => ({ text: r.querySelector('.sp-cname').firstChild.textContent.trim(),
  why: r.querySelector('.sp-why').textContent, badge: (r.querySelector('.badge') || {}).textContent || null,
  button: r.querySelector('button').textContent.trim() }));
out.names_heading = (d.querySelector('.modal .sp-h3') || {}).textContent || null;
const beforeName = requests.length;
await click(enames()[0].querySelector('[data-wrong="1"]'));
await wait(30);
out.name_posts = requests.slice(beforeName).filter((r) => r.method === 'POST').map((r) => r.body);
await click(d.querySelector('.modal .modal-close'));
await wait(30);
out.swaps_after_close = w.SWAPS;

const before = w.SWAPS;
await click(d.querySelector('[data-act="remove-term"]'));
await wait(30);
const form = d.querySelector('form[data-act="add-term"]');
form.querySelector('[name="term"]').value = '  PFL on Two ';
form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
await wait(30);
const accept = d.querySelector('[data-act="accept-term"]');
if (accept) await click(accept);
await wait(30);
out.term_puts = sent((r) => r.method === 'PUT' && /\/terms$/.test(r.path));
out.swaps_after_terms = w.SWAPS - before;

const sw = d.querySelector('input[data-act="replay"]');
sw.checked = !sw.checked;
sw.dispatchEvent(new w.Event('change', { bubbles: true }));
await wait(30);
await click(d.querySelector('[data-act="untie"]'));
await wait(30);
await click(d.querySelector('[data-act="tie"]'));
await wait(30);
out.offer = Array.from(d.querySelectorAll('.modal .sp-offer input')).map((i) => i.value);
d.querySelector('.modal .sp-offer input').checked = true;
await click(Array.from(d.querySelectorAll('.modal .modal-foot button')).find((b) => b.textContent.trim() === 'Add'));
await wait(30);
out.category_puts = sent((r) => r.method === 'PUT' && /\/categories$/.test(r.path));

console.log(JSON.stringify(out));
