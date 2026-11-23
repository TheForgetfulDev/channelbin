/* Drives the REAL picture-in-picture card and setup dialog (static/js/pip-setup.js and
   iptv-output.js over the markup the app rendered) in jsdom and prints what it observed as
   JSON. Called by tests/test_pip_setup_js.py, which owns every assertion - this file only
   reports. Faked: fetch (every request recorded; the pickers answer the app's own JSON, read
   from a file; a GET of the page answers the same render), setInterval and layout. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const PICKERS = JSON.parse(fs.readFileSync(`${DIR}/pickers.json`, 'utf8'));
const JS = ['util.js', 'pip-setup.js', 'iptv-output.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { errors.push(e.message); });
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:5000/iptv-output', virtualConsole: vc });
const w = dom.window;
const d = w.document;
fakeLayout(w);
w.scrollTo = () => {};
w.setInterval = () => 1;
const requests = [];
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
  const json = (status, payload) => Promise.resolve({ ok: status < 400, status, statusText: 'x',
    json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
  if (path === '/api/iptv-output/pip/pickers') return json(200, PICKERS);
  if (path === '/api/iptv-output/pip/would-play') {
    return json(200, { success: true, main: body.main ? { channel: 'Alpha', account: 'Provider one' } : null,
      inset: body.inset && body.inset.kind === 'group' ? { channel: 'Beta', account: 'Provider two' } : null,
      seats: body.main && body.inset ? 'While a TV watches it, it uses one connection now (Alpha on Provider one), and a second when the other picture comes on. Right now 2 of your 3 connections are free.' : '' });
  }
  if (method === 'POST' && path === '/api/iptv-output/pip') return json(200, { success: true, id: 9, name: body.name, num: 7, swapped_num: 0 });
  if (method === 'POST' && /^\/api\/iptv-output\/pip\/\d+$/.test(path)) return json(200, { success: true, name: body.name, kept: 1, cut: 0 });
  if (path.endsWith('/swapped-copy')) return json(200, { success: true, name: 'PiP 1', on: body.on, cut: 0, swapped_num: 8 });
  if (method === 'DELETE') return json(200, { success: true, name: 'PiP 1', cut: 0 });
  return json(200, { success: true });
};
JS.forEach((src) => w.eval(src));

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(); };
const change = async (el) => { el.dispatchEvent(new w.Event('change', { bubbles: true })); await wait(); };
const type = async (el, value) => { el.value = value; el.dispatchEvent(new w.Event('input', { bubbles: true })); await wait(); };
const modal = () => d.querySelector('.modal');
const foot = (label) => Array.from(modal().querySelectorAll('.modal-foot .btn')).find((b) => b.textContent.trim() === label);
const toasts = () => Array.from(d.querySelectorAll('.toast')).map((t) => t.textContent.trim());
const since = (n) => requests.slice(n).map((r) => `${r.method} ${r.path}`);
const view = () => ({
  open: Boolean(modal()),
  title: modal() ? modal().querySelector('.modal-head h2').textContent : '',
  picking: modal() ? Boolean(modal().querySelector('.pip-src')) : false,
  close_hidden: modal() ? modal().querySelector('.modal-close').hidden : null,
  back_hidden: modal() ? modal().querySelector('.modal-back').hidden : null,
  foot_hidden: modal() ? modal().querySelector('.modal-foot').hidden : null,
});
const escape = async () => { d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await wait(); };
const pick = (which) => modal().querySelector(`[data-open="${which}"]`);
const out = { errors };

/* Create: nothing chosen, so Create says why it cannot. */
await click(d.querySelector('[data-act="pip-add"]'));
await wait(30);
out.create = view();
out.create_note = modal().footNote.textContent;
out.create_disabled = foot('Create').getAttribute('aria-disabled');
let mark = requests.length;
await click(foot('Create'));
out.create_refused_posts = since(mark);

/* A picker is a step: Escape, the backdrop and the back arrow each return to the main view. */
await click(pick('main'));
out.picker = view();
out.picker_tabs = Array.from(modal().querySelectorAll('.tab')).map((t) => t.textContent.trim());
out.picker_days = Array.from(modal().querySelectorAll('.pip-day')).map((x) => x.textContent.trim());
await escape();
out.after_escape = view();
await click(pick('main'));
await click(modal().querySelector('.modal-backdrop'));
out.after_backdrop = view();
await click(pick('main'));
await click(modal().querySelector('.modal-back'));
out.after_back = view();

/* Choosing a game returns to the main view with it named, in the row and in the stand-in. */
await click(pick('main'));
const firstGame = modal().querySelector('input[data-k="game"]');
firstGame.checked = true;
await change(firstGame);
out.after_pick = view();
out.main_row = pick('main').querySelector('.pip-pr-v').textContent.trim();
out.standin_big = modal().querySelector('.pip-pane:not(.pip-inset) .pip-ph-t').textContent;

/* The other picture: the game already taken is disabled; a channel is chosen on its tab. */
await click(pick('inset'));
const taken = modal().querySelector(`input[data-k="game"][data-ref="${firstGame.dataset.ref}"]`);
out.taken_disabled = taken.disabled;
out.taken_says = taken.closest('label').textContent.includes('Already the other picture');
await click(modal().querySelector('[data-tab="inset"][data-k="channel"]'));
out.channel_rows = Array.from(modal().querySelectorAll('.pip-opt .t')).map((x) => x.textContent);
await type(modal().querySelector('[data-filter="inset"]'), 'bet');
out.channel_filtered = Array.from(modal().querySelectorAll('.pip-opt .t')).map((x) => x.textContent);
const beta = modal().querySelector('input[data-k="channel"]');
beta.checked = true;
await change(beta);
out.note_without_name = modal().footNote.textContent;

/* The layout redraws the stand-in from the server's own boxes. */
const boxOf = () => modal().querySelector('.pip-inset').getAttribute('style');
out.box_corner = boxOf();
await click(modal().querySelector('[data-corner="tl"]'));
out.box_top_left = boxOf();
await click(modal().querySelector('[data-layout="side"]'));
out.box_side = boxOf();
out.corner_disabled_side_by_side = modal().querySelector('[data-corner="tl"]').getAttribute('aria-disabled');

/* A name makes it creatable; Create posts once and the toast names its number. Typing the
   name redraws nothing that asks what the pictures would play from again. */
const wouldAsks = () => requests.filter((r) => r.path === '/api/iptv-output/pip/would-play').length;
const asksBeforeName = wouldAsks();
await type(d.getElementById('pip-name'), 'Race day');
out.would_asks_for_a_name = wouldAsks() - asksBeforeName;
out.note_ready = modal().footNote.textContent;
mark = requests.length;
await click(foot('Create'));
await wait(60);
out.create_posts = since(mark);
out.create_body = requests[mark].body;
out.closed_after_create = !modal();
out.create_toast = toasts().find((t) => t.includes('Race day')) || '';

/* Change opens on the stored setup; on the main view the backdrop closes the dialog. */
const tr = d.querySelector('tr[data-pip][data-set="1"]');
await click(tr.querySelector('[data-act="pip-setup"]'));
await wait(30);
out.change = view();
out.change_name = d.getElementById('pip-name').value;
out.change_rows = [pick('main'), pick('inset')].map((b) => b.querySelector('.pip-pr-v .t').textContent);
out.would_main = pick('main').querySelector('[data-would="main"]').textContent;
out.would_inset = pick('inset').querySelector('[data-would="inset"]').textContent;
out.would_seats = modal().querySelector('[data-pip-seats]').textContent;
out.would_seats_hidden = modal().querySelector('[data-pip-seats]').hidden;
out.would_body = requests.filter((r) => r.path === '/api/iptv-output/pip/would-play').pop().body;
await click(modal().querySelector('.modal-backdrop'));
out.backdrop_closes_main_view = !modal();

/* A search: Use this search is off until words are typed, and lists what matches now. */
await click(tr.querySelector('[data-act="pip-setup"]'));
await wait(30);
await click(pick('inset'));
await click(modal().querySelector('[data-tab="inset"][data-k="search"]'));
out.use_search_off = modal().querySelector('[data-dlg="use-search"]').disabled;
await type(modal().querySelector('[data-q="inset"]'), 'alp');
out.search_hits = Array.from(modal().querySelectorAll('.pip-opt .t')).map((x) => x.textContent);
await click(modal().querySelector('[data-dlg="use-search"]'));
out.search_row = pick('inset').querySelector('.pip-pr-v').textContent.trim();
mark = requests.length;
await click(foot('Save'));
await wait(60);
out.save_posts = since(mark);
out.save_body = requests[mark].body;
out.save_toast = toasts().find((t) => t.startsWith('Saved.')) || '';

/* The row's own actions. */
const row = () => d.querySelector('tr[data-pip][data-set="1"]');
mark = requests.length;
const twin = row().querySelector('[data-act="pip-twin"]');
twin.checked = !twin.checked;
await change(twin);
await wait(40);
out.twin_posts = since(mark);
out.twin_body = requests[mark].body;
mark = requests.length;
await click(row().querySelector('[data-act="pip-remove"]'));
out.remove_asks = modal() ? modal().querySelector('.modal-body').textContent.trim() : '';
await click(foot('Cancel'));
out.remove_cancel_posts = since(mark);
await click(row().querySelector('[data-act="pip-remove"]'));
await click(foot('Remove'));
await wait(40);
out.remove_posts = since(mark);
const empty = d.querySelector('tr[data-pip][data-set="0"]');
out.empty_swap_disabled = empty.querySelector('[data-act="pip-swap"]').disabled;
out.empty_twin_disabled = empty.querySelector('[data-act="pip-twin"]').disabled;

console.log(JSON.stringify(out));
