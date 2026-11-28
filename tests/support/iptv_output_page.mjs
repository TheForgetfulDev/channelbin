/* Drives the REAL IPTV Output page (static/js/iptv-output.js over the markup the app rendered)
   in jsdom and prints what it observed as JSON. Called by tests/test_iptv_output_page_js.py,
   which owns every assertion - this file only reports. Faked: fetch (every request recorded;
   a GET of the page answers the same render), the clipboard, setInterval (recorded, so the
   live refresh's turn is run by hand) and layout (jsdom_layout.mjs). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const JS = ['util.js', 'iptv-output.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const PASSWORD = 'p&w=1';
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
const intervals = [];
w.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
const copied = [];
Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: (t) => { copied.push(t); return Promise.resolve(); } } });
const requests = [];
const REFUSE = new Set();
// What a login's Choose channels read answers (routes/iptv_output.py::login_channels): two
// categories, a row off the output, a picture-in-picture channel with its swapped copy.
const CHANNELS = { success: true, name: 'Kim', everything: false, picked: [2], rows: [
  { id: 2, name: 'Alpha', category: 'News', num: 1, off: '', swapped: false },
  { id: 4, name: 'Beta', category: 'News', num: 0, off: 'Taken off the output', swapped: false },
  { id: 3, name: 'Game feeds', category: 'Sports', num: 2, off: '', swapped: false },
  { id: 2000000002, name: 'PiP 1', category: 'ChannelBin', num: 3, off: '', swapped: true },
] };
// What a login's Choose accounts read answers (routes/iptv_output.py::login_accounts): one
// account the output plays from and one it leaves out, the login on the output's list.
const ACCOUNTS = { success: true, name: 'Kim', own: false, picked: [], accounts: [
  { id: 1, name: 'Acct', on_output: true },
  { id: 2, name: 'Other', on_output: false },
] };
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
  const json = (status, payload) => Promise.resolve({ ok: status < 400, status, statusText: 'x',
    json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
  if (/^\/api\/iptv-output\/logins\/\d+\/password$/.test(path)) return json(200, { success: true, password: PASSWORD });
  if (REFUSE.has(path)) return json(400, { error: 'Refused by the server.' });
  if (/^\/api\/iptv-output\/logins\/\d+\/channels$/.test(path)) return json(200, CHANNELS);
  if (/^\/api\/iptv-output\/logins\/\d+\/accounts$/.test(path)) return json(200, ACCOUNTS);
  if (method === 'POST' && path.startsWith('/api/iptv-output/rows/')) return json(200, { success: true, name: 'x', on: body.on, cut: body.on ? 0 : 1 });
  return json(200, { success: true });
};
w.eval(JS[0]);
w.eval(JS[1]);

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(); };
const change = async (el) => { el.dispatchEvent(new w.Event('change', { bubbles: true })); await wait(); };
const flip = async (input) => { input.checked = !input.checked; await change(input); };
const visible = () => Array.from(d.querySelectorAll('#of-rows tr[data-feed]')).filter((tr) => !tr.hidden).map((tr) => tr.dataset.name);
const modal = () => d.querySelector('.modal');
const modalButton = (label) => Array.from(modal().querySelectorAll('.modal-foot .btn')).find((b) => b.textContent.trim() === label);
const since = (n) => requests.slice(n).map((r) => `${r.method} ${r.path}`);
const toasts = () => Array.from(d.querySelectorAll('.toast')).map((t) => t.textContent.trim());
const row = (name) => d.querySelector(`#of-rows tr[data-name="${name}"]`);
const out = { errors, live_ms: intervals.map((i) => i.ms) };

/* The channel list's tools */
out.all_rows = visible();
const q = d.getElementById('of-q');
q.value = 'BET';
q.dispatchEvent(new w.Event('input', { bubbles: true }));
out.searched = visible();
out.none_hidden_on_a_hit = d.getElementById('of-none').hidden;
await click(d.querySelector('[data-show="hidden"]'));
out.hidden_chip_rows = visible();
out.none_shown_on_no_hit = !d.getElementById('of-none').hidden;
out.hidden_chip_active = d.querySelector('[data-show="hidden"]').classList.contains('active');
await click(d.querySelector('[data-show="all"]'));

/* Paging, two to a page here whatever the server's own size. */
const pager = d.getElementById('of-chan-pager');
const pagerText = () => d.getElementById('of-chan-n').textContent;
const pagerBtn = (dir) => pager.querySelector(`[data-dir="${dir}"]`);
q.value = '';
q.dispatchEvent(new w.Event('input', { bubbles: true }));
out.pager_hidden_on_one_page = pager.hidden;
d.getElementById('of-rows').dataset.perPage = '2';
await click(d.querySelector('[data-show="all"]'));
out.page_one = { rows: visible(), text: pagerText(), hidden: pager.hidden, prev_off: pagerBtn(-1).disabled, next_off: pagerBtn(1).disabled };
await click(pagerBtn(1));
out.page_two = { rows: visible(), text: pagerText(), prev_off: pagerBtn(-1).disabled, next_off: pagerBtn(1).disabled };
q.value = 'alp';
q.dispatchEvent(new w.Event('input', { bubbles: true }));
out.search_from_page_two = { rows: visible(), pager_hidden: pager.hidden };
q.value = '';
q.dispatchEvent(new w.Event('input', { bubbles: true }));
await click(pagerBtn(1));
await click(pagerBtn(-1));
out.back_to_page_one = visible();
d.getElementById('of-rows').dataset.perPage = '25';
q.value = 'BET';
q.dispatchEvent(new w.Event('input', { bubbles: true }));

/* A switch on an unwatched row: one POST, then the page is swapped, and the search is kept. */
let mark = requests.length;
await flip(row('Beta').querySelector('input'));
await wait(40);
out.unwatched_off = since(mark);
out.unwatched_body = requests[mark].body;
out.modal_for_unwatched = Boolean(modal());
out.search_kept_after_swap = { value: d.getElementById('of-q').value, rows: visible() };
out.hide_toast = toasts().some((t) => t.includes('is hidden from the IPTV output'));
q.value = '';
q.dispatchEvent(new w.Event('input', { bubbles: true }));

/* A watched row asks first. Cancel leaves it on and posts nothing. */
mark = requests.length;
await flip(row('Alpha').querySelector('input'));
out.watched_asks = modal() ? modal().querySelector('.modal-body').textContent.trim() : '';
await click(modalButton('Cancel'));
out.cancel_posts = since(mark);
out.cancel_leaves_on = row('Alpha').querySelector('input').checked;
await flip(row('Alpha').querySelector('input'));
await click(modalButton('Hide'));
await wait(40);
out.confirm_posts = since(mark);

/* A refused switch goes back to where it was. */
const gameFeed = row('Game feeds').dataset.feed;
REFUSE.add(`/api/iptv-output/rows/${gameFeed}`);
await flip(row('Game feeds').querySelector('input'));
await wait(40);
out.refused_back_on = row('Game feeds').querySelector('input').checked;
out.refused_toast = toasts().some((t) => t.includes('Refused by the server.'));

/* Connection details */
await click(d.querySelector('[data-tab="playlist"]'));
out.playlist_shown = !d.querySelector('[data-pane="playlist"]').hidden && d.querySelector('[data-pane="xtream"]').hidden;
const m3u = d.querySelector('[data-pane="playlist"] .of-conn');
out.m3u_masked = m3u.querySelector('.v').textContent;
await click(m3u.querySelector('[data-act="reveal"]'));
out.m3u_revealed = m3u.querySelector('.v').textContent;
await click(m3u.querySelector('[data-act="reveal"]'));
out.m3u_masked_again = m3u.querySelector('.v').textContent;
await click(m3u.querySelector('[data-act="copy"]'));
const pw = d.querySelector('[data-pane="xtream"] [data-secret-text]').closest('.of-conn');
await click(pw.querySelector('[data-act="reveal"]'));
out.xtream_revealed = pw.querySelector('.v').textContent;
await click(pw.querySelector('[data-act="reveal"]'));
out.xtream_masked_again = pw.querySelector('.v').textContent;
await click(d.querySelector('[data-pane="xtream"] [data-secret] '));
await click(d.querySelector('[data-pane="xtream"] [data-act="copy"]'));
out.copied = copied.slice();

/* The Connect card shows one login at a time, and the picker at its top swaps which; the eye
   asks for the password of the login shown (dev/changelog/1781). */
const connPick = d.getElementById('of-conn-login');
const shownUsers = () => Array.from(d.querySelectorAll('#of-connect [data-pane="xtream"] [data-conn-login]'))
  .filter((el) => !el.hidden).map((el) => el.querySelector('[data-copy-value]').textContent);
out.conn_picker = connPick ? Array.from(connPick.options).map((o) => o.textContent) : null;
out.conn_shown_first = shownUsers();
connPick.value = connPick.options[1].value;
await change(connPick);
out.conn_shown_picked = shownUsers();
mark = requests.length;
await click(d.querySelector('#of-connect [data-pane="xtream"] [data-conn-login]:not([hidden]) [data-act="reveal"]'));
out.conn_reveal = { paths: requests.slice(mark).map((r) => r.path), picked: connPick.value };

/* Accounts */
mark = requests.length;
await flip(d.querySelector('#of-accounts input[data-act="account"]'));
await wait(40);
out.account_post = requests[mark] ? { path: requests[mark].path, body: requests[mark].body } : null;

/* Settings card: a number is saved as a number, through the Settings page's route. */
mark = requests.length;
const max = d.querySelector('[data-setting-path="integrations.output_feed.max_viewers"]');
max.value = '5';
// The next row is already being typed in when the save lands.
const idle = d.querySelector('[data-setting-path="integrations.output_feed.idle_seconds"]');
idle.value = '4';
await change(max);
await wait(40);
out.half_typed_survives_a_save = d.querySelector('[data-setting-path="integrations.output_feed.idle_seconds"]').value;
out.saved_field_shows_what_was_saved = d.querySelector('[data-setting-path="integrations.output_feed.max_viewers"]').value;
out.setting_post = requests[mark] ? { path: requests[mark].path, body: requests[mark].body } : null;
mark = requests.length;
const modeSel = d.querySelector('[data-setting-path="integrations.output_feed.mode"]');
modeSel.value = 'redirect';
await change(modeSel);
await wait(40);
out.mode_post = requests[mark] ? requests[mark].body : null;

/* Recent plays: a chip is a link, swapped through the page's one queue. */
mark = requests.length;
await click(d.querySelector('#of-recent a[data-hist="refused"]'));
await wait(40);
out.chip_fetch = since(mark);
out.chip_address = w.location.pathname + w.location.search;
w.history.replaceState(null, '', '/iptv-output');

/* Stop this play */
mark = requests.length;
await click(d.querySelector('[data-act="stop"]'));
out.stop_asks = modal() ? modal().querySelector('.modal-head h2').textContent.trim() : '';
await click(modalButton('Stop'));
await wait(40);
out.stop_posts = since(mark);

/* Choose channels (dev/changelog/1764): the list comes from the login's own read, a search
   narrows it, "Pick all shown" picks only what the search left, and Save posts feed ids. */
const luBoxes = () => Array.from(modal().querySelectorAll('.of-lu-list input[type="checkbox"]'));
const luTicked = () => luBoxes().filter((b) => b.checked).map((b) => Number(b.value));
const luSearch = (text) => {
  const box = modal().querySelector('.of-lu-tools .search');
  box.value = text;
  box.dispatchEvent(new w.Event('input', { bubbles: true }));
};
const luOpen = async () => {
  // Asked again each time: a save swaps the card for the server's render.
  await click(d.querySelector('#of-logins tr[data-login] [data-act="login-channels"]'));
  await wait(40);
};
mark = requests.length;
await luOpen();
out.lu_fetch = since(mark);
out.lu_initial = {
  title: modal().querySelector('.modal-head h2').textContent.trim(),
  picked: luTicked(),
  pick_shown: !modal().querySelector('.of-lu-pick').hidden,
  n: modal().querySelector('.of-lu-n').textContent,
  heads: Array.from(modal().querySelectorAll('.of-lu-head')).map((h) => h.textContent),
  notes: Array.from(modal().querySelectorAll('.of-lu-list .check-row')).map((l) => l.textContent.replace(/\s+/g, ' ').trim()),
};
luSearch('news');
out.lu_searched_category = luBoxes().filter((b) => !b.closest('label').hidden).map((b) => Number(b.value));
luSearch('ga');
out.lu_searched = luBoxes().filter((b) => !b.closest('label').hidden).map((b) => Number(b.value));
out.lu_heads_shown = Array.from(modal().querySelectorAll('[data-lu-cat]')).filter((c) => !c.hidden).map((c) => c.querySelector('.of-lu-head').textContent);
await click(modal().querySelector('[data-lu="all"]'));
out.lu_after_pick_all = { picked: luTicked(), n: modal().querySelector('.of-lu-n').textContent };
luSearch('');
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.lu_post = requests.slice(mark).filter((r) => r.method === 'POST').map((r) => ({ path: r.path, body: r.body }));
out.lu_closed = !modal();
// Nothing picked is refused before anything is sent.
await luOpen();
await click(modal().querySelector('[data-lu="none"]'));
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.lu_empty = { posts: requests.slice(mark).filter((r) => r.method === 'POST').length, open: Boolean(modal()),
                 toast: toasts().some((t) => t.includes('Pick at least one channel')) };
// Everything hides the list and posts null.
const all = modal().querySelector('input[name="of-lu-mode"][value="all"]');
all.checked = true;
await change(all);
out.lu_everything_hides_list = modal().querySelector('.of-lu-pick').hidden;
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.lu_everything_post = requests.slice(mark).filter((r) => r.method === 'POST').map((r) => r.body);

/* Choose accounts (dev/changelog/1765): the login's own read, the list hidden while it plays
   from the output's accounts, nothing picked refused, Save posts account ids or null. */
const laOpen = async () => {
  await click(d.querySelector('#of-logins tr[data-login] [data-act="login-accounts"]'));
  await wait(40);
};
mark = requests.length;
await laOpen();
out.la_fetch = since(mark);
out.la_initial = {
  title: modal().querySelector('.modal-head h2').textContent.trim(),
  list_hidden: modal().querySelector('.of-la-pick').hidden,
  rows: Array.from(modal().querySelectorAll('.of-la-pick .check-row')).map((l) => l.textContent.replace(/\s+/g, ' ').trim()),
};
const pick = modal().querySelector('input[name="of-la-mode"][value="pick"]');
pick.checked = true;
await change(pick);
out.la_list_shown = !modal().querySelector('.of-la-pick').hidden;
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.la_empty = { posts: requests.slice(mark).filter((r) => r.method === 'POST').length, open: Boolean(modal()),
                 toast: toasts().some((t) => t.includes('Pick at least one account')) };
modal().querySelector('.of-la-pick input[value="2"]').checked = true;
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.la_post = requests.slice(mark).filter((r) => r.method === 'POST').map((r) => ({ path: r.path, body: r.body }));
out.la_closed = !modal();
await laOpen();
mark = requests.length;
await click(modalButton('Save'));
await wait(40);
out.la_output_post = requests.slice(mark).filter((r) => r.method === 'POST').map((r) => r.body);

/* Change a login: prefilled but never with its password, which an empty field keeps, and its two
   home-only switches shown as they are and sent as they are left (dev/changelog/1781). */
mark = requests.length;
const loginRow = d.querySelector('#of-logins tr[data-login][data-user="tv"]');
await click(loginRow.querySelector('[data-act="login-edit"]'));
out.login_prefill = { user: d.getElementById('of-l-user').value, pass: d.getElementById('of-l-pass').value,
  direct: d.getElementById('of-l-direct').checked, whole: d.getElementById('of-l-whole').checked };
d.getElementById('of-l-direct').checked = false;
await click(modalButton('Save'));
await wait(40);
const loginPost = requests.slice(mark).find((r) => r.method === 'POST');
out.login_post = loginPost ? { path: loginPost.path, body: loginPost.body, row: loginRow.dataset.login } : null;

/* The live refresh swaps only what moves, and waits while a dialog is open. */
mark = requests.length;
intervals[0].fn();
await wait(40);
out.tick_fetches = since(mark);
await click(d.querySelector('[data-act="power"]'));
out.power_asks = modal() ? modal().querySelector('.modal-body').textContent.trim() : '';
mark = requests.length;
intervals[0].fn();
await wait(40);
out.tick_under_dialog = since(mark);
await click(modalButton('Turn off'));
await wait(40);
out.power_post = requests.slice(mark).find((r) => r.method === 'POST') || null;

/* Hide on the address notice stores the version the page was handed, under the pref it names,
   then swaps the cards so the template draws the hidden state. */
mark = requests.length;
const hideBtn = d.querySelector('[data-act="hide-address-notice"]');
out.hide_offered = hideBtn ? { pref: hideBtn.dataset.pref, version: hideBtn.dataset.version } : null;
if (hideBtn) await click(hideBtn);
await wait(40);
out.hide_requests = requests.slice(mark).map((r) => ({ method: r.method, path: r.path, body: r.body }));

console.log(JSON.stringify(out));
