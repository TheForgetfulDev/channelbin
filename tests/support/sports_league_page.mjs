/* Drives the REAL Sports Schedules league page (static/js/sports.js over the markup the app rendered)
   in jsdom and prints what it observed as JSON: the listings dialog's closed groups, event channels and Wrong match,
   and the Matching card's names, categories and skip words (mockups 51, 59). Called by
   tests/test_sports_league_matches.py, which owns every assertion - this file only reports. Faked:
   fetch (answers per request) and swapFromServer. The page also carries the shared record dialog
   (#record-modal and #active-rec-modal, hidden, each with an id), so a dialog this drives is a .modal with no id. */
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
  if (method === 'PUT' && /\/categories$/.test(path) && body.add) {
    // As the route answers an add: what was tied, and which arrived with Replays only on. The
    // row already on the page stands in for a new one, since the swap is stubbed.
    payload = { success: true, added: body.add.concat(['PFL Package']), replay_on: body.add };
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
};
w.eval(JS[0]);
w.eval('window.SWAPS = 0; swapFromServer = function () { SWAPS += 1; return Promise.resolve(); };');
w.eval(JS[1]);

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(); };
const out = { errors };
const groups = () => Array.from(d.querySelectorAll('.modal:not([id]) .sp-group'));
const sent = (fn) => requests.filter(fn).map((r) => ({ method: r.method, path: r.path, body: r.body }));

await click(d.querySelector('.sp-onguide'));
await wait(30);
out.dialog_title = d.querySelector('.modal:not([id]) h2').textContent;
out.groups = groups().length;
out.closed = groups().every((g) => !g.classList.contains('open')) && d.querySelectorAll('.modal:not([id]) .sp-group .sp-chan').length === 0;
out.group_counts = groups().map((g) => g.querySelector('.sp-why strong').textContent);
out.group_buttons = groups().map((g) => {
  const b = g.querySelector('[data-wrong="1"]');
  return b ? { text: b.textContent.trim(), danger: b.classList.contains('btn-danger') } : null;
});
out.intro = d.querySelector('.modal:not([id]) .sp-dialog-body').textContent.replace(/\s+/g, ' ');

// The Skipped section (mockup 59, dev/changelog/1634): behind Show, which turns into Hide.
const skipToggle = () => d.querySelector('.modal:not([id]) [data-skipped]');
const skippedRows = () => Array.from(d.querySelectorAll('.modal:not([id]) .sp-skipped'));
out.skipped_closed = { button: skipToggle().textContent.trim(), rows: skippedRows().length };
await click(skipToggle());
out.skipped_open = { button: skipToggle().textContent.trim(), expanded: skipToggle().getAttribute('aria-expanded'),
  rows: skippedRows().map((r) => ({ title: r.querySelector('.sp-gtitle').textContent, why: r.querySelector('.sp-why').textContent })) };
await click(skipToggle());
out.skipped_hidden = { button: skipToggle().textContent.trim(), rows: skippedRows().length };

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
const enames = () => Array.from(d.querySelectorAll('.modal:not([id]) .sp-ename'));
out.names = enames().map((r) => ({ text: r.querySelector('.sp-cname').firstChild.textContent.trim(),
  why: r.querySelector('.sp-why').textContent, badge: (r.querySelector('.badge') || {}).textContent || null,
  button: r.querySelector('button').textContent.trim() }));
out.names_heading = (d.querySelector('.modal:not([id]) .sp-h3') || {}).textContent || null;
const beforeName = requests.length;
await click(enames()[0].querySelector('[data-wrong="1"]'));
await wait(30);
out.name_posts = requests.slice(beforeName).filter((r) => r.method === 'POST').map((r) => r.body);
await click(d.querySelector('.modal:not([id]) .modal-close'));
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
out.offer = Array.from(d.querySelectorAll('.modal:not([id]) .sp-offer input')).map((i) => i.value);
d.querySelector('.modal:not([id]) .sp-offer input').checked = true;
await click(Array.from(d.querySelectorAll('.modal:not([id]) .modal-foot button')).find((b) => b.textContent.trim() === 'Add'));
await wait(30);
out.category_puts = sent((r) => r.method === 'PUT' && /\/categories$/.test(r.path));
out.add_toast = d.getElementById('app-toast').textContent;
const marked = d.querySelector('tr[data-cat="PFL Package"]');
out.new_row = { marked: marked.classList.contains('sp-cat-new'), badge: (marked.querySelector('.sp-cat-newbadge') || {}).textContent || null };
// A tap that lands while the list is still settling is put back and not sent.
const late = d.querySelector('input[data-act="replay"]');
const was = late.checked;
late.checked = !was;
late.dispatchEvent(new w.Event('change', { bubbles: true }));
await wait(30);
out.settling = { restored: late.checked === was, toast: d.getElementById('app-toast').textContent,
  puts: sent((r) => r.method === 'PUT' && /\/categories$/.test(r.path)).length };

// The words and lengths (mockup 59, dev/changelog/1634): each change is one request, then a swap.
const beforeSkips = requests.length;
await click(d.querySelector('[data-act="remove-skip"][data-word="pre game"]'));
await wait(30);
const skipForm = d.querySelector('form[data-act="add-skip"]');
skipForm.querySelector('[name="word"]').value = '  Gameday ';
skipForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
await wait(30);
const len = d.querySelector('input[data-skip-length="game"]');
len.value = '75';
len.dispatchEvent(new w.Event('change', { bubbles: true }));
await wait(30);
len.value = '';
len.dispatchEvent(new w.Event('change', { bubbles: true }));
await wait(30);
await click(d.querySelector('[data-act="restore-skips"]'));
await wait(30);
out.restore_confirm = d.querySelector('.modal:not([id]) .modal-body').textContent.replace(/\s+/g, ' ').trim();
await click(Array.from(d.querySelectorAll('.modal:not([id]) .modal-foot button')).find((b) => b.textContent.trim() === 'Restore defaults'));
await wait(30);
out.skip_requests = requests.slice(beforeSkips).filter((r) => r.method !== 'GET').map((r) => ({ method: r.method, path: r.path, body: r.body }));

await click(d.querySelector('[data-act="remove-team-name"]'));
await wait(30);
await click(d.querySelector('[data-act="team-name"]'));
await wait(30);
const pick = d.querySelector('.modal:not([id]) #sp-tn-team');
out.team_options = Array.from(pick.options).map((o) => o.textContent);
pick.value = pick.options[1].value;
pick.dispatchEvent(new w.Event('change', { bubbles: true }));
out.team_theirs = d.querySelector('.modal:not([id]) #sp-tn-theirs').textContent;
d.querySelector('.modal:not([id]) #sp-tn-name').value = '  Harborview-on-Sea ';
await click(Array.from(d.querySelectorAll('.modal:not([id]) .modal-foot button')).find((b) => b.textContent.trim() === 'Add'));
await wait(30);
out.team_puts = sent((r) => r.method === 'PUT' && /\/teams\/\d+\/names$/.test(r.path));

console.log(JSON.stringify(out));
// The page's live refresh keeps a timer running, so node would otherwise wait on it forever.
process.exit(0);
