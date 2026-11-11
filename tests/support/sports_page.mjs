/* Drives the REAL Sports Schedules list (static/js/sports.js over the markup the app rendered) in
   jsdom and prints what it observed as JSON. Called by tests/test_sports_page_js.py, which owns every
   assertion - this file only reports. Faked: fetch (answers per request, and a GET of the page
   can be held back and marked through GET_PLAN), followHref, setInterval (recorded,
   so the live refresh's turn is run by hand) and layout (jsdom_layout.mjs). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/list.html`, 'utf8');
const JS = ['util.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const navigations = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => {
  if (/navigation|reload/i.test(e.message)) navigations.push(e.message);
  else errors.push(e.message);
});
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:5000/sports', virtualConsole: vc });
const w = dom.window;
const d = w.document;
fakeLayout(w);
w.scrollTo = () => {}; // the scroll lock's restore, once a menu or dialog reads as open
const intervals = [];
w.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
const requests = [];
// Each GET of the page takes the next entry: { mark, gate } answers, once the promise `gate` (if
// any) settles, with the page's #sports-body marked data-v="<mark>"; { fail: true } answers 500.
const GET_PLAN = [];
const pageCopy = (mark) => HTML.replace('id="sports-body"', `id="sports-body" data-v="${mark}"`);
const ANSWERS = {
  'POST /api/sports/lookup': (body) => {
    if (body.text === 'soon') return [409, { error: 'ChannelBin makes one call at a time to TheSportsDB, and one just went out. Try again in 8 seconds.' }];
    if (body.text === 'two') {
      const league = { tsdb_id: 9001, name: 'Pretend League', sport: 'Pretend Ball', season: null };
      return [200, { success: true, skipped: ['Old Reds'], choices: [
        { team_id: 11, team: 'Pretendville Reds', league }, { team_id: 12, team: 'Reds Reserves', league }] }];
    }
    if (body.text === 'xyzzy') return [404, { error: 'TheSportsDB found no team called "xyzzy". A league with no teams, such as a racing series, can be added with its number or a link to its page on TheSportsDB.' }];
    return [200, { success: true, league: { tsdb_id: 9001, name: 'Pretend League', sport: 'Pretend Ball', season: '2099' },
                   team: body.text === 'reds' ? { team_id: 11, team: 'Pretendville Reds' } : undefined,
                   best_match: body.text === 'reds' ? true : undefined }];
  },
  'POST /api/sports/leagues': () => [200, { success: true, id: 1 }],
};
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
  if (method === 'GET' && path === '/sports' && GET_PLAN.length) {
    const plan = GET_PLAN.shift();
    if (plan.fail) return Promise.resolve({ ok: false, status: 500, text: () => Promise.resolve('') });
    return Promise.resolve(plan.gate).then(() => ({ ok: true, status: 200, text: () => Promise.resolve(pageCopy(plan.mark)) }));
  }
  const answer = ANSWERS[`${method} ${path}`];
  if (!answer) return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ success: true }), text: () => Promise.resolve(HTML) });
  const [status, payload] = answer(body);
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(payload),
                           text: () => Promise.resolve(HTML) });
};
w.eval(JS[0]);
w.eval('window.OPENED = []; followHref = function (e, url) { OPENED.push(url); };');
w.eval(JS[1]);

const click = async (el) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(); };
const order = () => Array.from(d.querySelectorAll('#sp-rows .row')).map((r) => r.dataset.name);
const out = { errors, requests };

out.default_order = order();
await click(d.querySelector('[data-sort="name"]'));
out.by_name = order();
out.sorted_marked = d.querySelector('[data-sort="name"]').classList.contains('sorted');
await click(d.querySelector('[data-sort="name"]'));
out.by_name_flipped = order();
await click(d.querySelector('[data-sort="count"]'));
out.by_count = order();
out.chip = d.getElementById('sp-sort-chip').textContent.trim();

await click(d.querySelector('#sp-rows .row .rl-sum'));
out.opened = w.OPENED.slice();
w.OPENED.length = 0;
await click(d.querySelector('#sp-rows .row .c-actions button'));
out.kebab_opened_page = w.OPENED.length;
out.kebab_menu_open = Boolean(d.querySelector('#sp-rows .menu.open'));

/* ---- The live refresh: a turn is run by hand through the interval the script set ---------- */
const live = intervals.find((i) => i.ms === 30000);
out.live_interval = Boolean(live);
const pageGets = () => requests.filter((r) => r.method === 'GET' && r.path === '/sports').length;
const mark = () => d.getElementById('sports-body').dataset.v || null;
const turn = async (ms = 30) => {
  const before = pageGets();
  live.fn();
  await wait(ms);
  return pageGets() - before;
};

// Without a timer there is nothing to drive; the tests read the missing observations as red.
if (live) {
  out.live_menu_gets = await turn();
  w.eval('closeMenus()');

  await click(d.querySelector('[data-sort="name"]'));
  await click(d.querySelector('[data-sort="name"]'));
  out.live_sorted = order();
  GET_PLAN.push({ mark: 'idle' });
  out.live_idle = { gets: await turn(), mark: mark(), order: order() };

  Object.defineProperty(d, 'hidden', { configurable: true, get: () => true });
  out.live_hidden_gets = await turn();
  delete d.hidden;
  GET_PLAN.push({ mark: 'shown' });
  d.dispatchEvent(new w.Event('visibilitychange'));
  await wait(30);
  out.live_shown_mark = mark();

  const field = d.createElement('input');
  d.getElementById('sports-body').append(field);
  field.value = 'half typed';
  out.live_typing_gets = await turn();
  field.remove();

  w.getSelection().selectAllChildren(d.querySelector('#sp-rows .rl-name'));
  out.live_selection_gets = await turn();
  w.getSelection().removeAllRanges();

  GET_PLAN.push({ fail: true });
  const navsBefore = navigations.length;
  out.live_fail_gets = await turn();
  out.live_fail = { navigations: navigations.length - navsBefore, rows: d.querySelectorAll('#sp-rows .row').length };

  // A timed swap still in flight when an action refreshes: the action's render must be the one left.
  let release;
  GET_PLAN.push({ mark: 'timed', gate: new Promise((r) => { release = r; }) }, { mark: 'action' });
  live.fn();
  await wait(5);
  await click(d.querySelector('#sp-rows .row .c-actions button'));
  await click(d.querySelector('#sp-rows .menu.open [data-act="stop"]'));
  await click(Array.from(d.querySelectorAll('.modal-foot button')).find((b) => b.textContent.trim() === 'Stop following'));
  const until = async (ok) => { for (let i = 0; i < 200 && !ok(); i++) await wait(10); };
  await wait(30);
  release();
  // The action's GET goes out only once the timed swap is done; give its own swap time to land.
  await until(() => GET_PLAN.length === 0);
  await wait(100);
  out.live_race = { mark: mark(), deletes: requests.filter((r) => r.method === 'DELETE').length };
}
w.eval('closeMenus()');

out.add_button = d.getElementById('sp-add').textContent.trim();
await click(d.getElementById('sp-add'));
// A missing control is recorded as a missing observation, so each test fails on its own.
const press = async (el) => { if (el) await click(el); };
const footButtons = () => Array.from(d.querySelectorAll('.modal-foot button'));
const foot = () => footButtons().map((b) => b.textContent.trim());
const footButton = (label) => footButtons().find((b) => b.textContent.trim() === label);
out.add_title = d.querySelector('.modal-head h2').textContent;
const hint = d.querySelector('.modal-body .form-hint a');
out.hint_link = hint ? { text: hint.textContent, target: hint.target, rel: hint.rel } : null;
out.picks = d.querySelectorAll('.sp-pick').length;
out.entry_foot = foot();
out.search_disabled_empty = footButton('Search') ? footButton('Search').disabled : null;
out.body_buttons = d.querySelectorAll('.modal-body button.btn').length;
const type = async (text) => {
  const input = d.getElementById('sp-entry');
  input.value = text;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(2);
};
await type('motorsport');
out.filtered_picks = d.querySelectorAll('.sp-pick').length;
out.calls_while_typing = requests.length;
// The search: the footer's Search, or (before dev/changelog/1497) the body's lookup button.
const searchButton = () => footButton('Search') || d.querySelector('[data-lookup]');
await type('NHL');
out.search_disabled_shipped_name = footButton('Search') ? footButton('Search').disabled : null;
out.dialog_text = d.querySelector('.modal-panel').textContent;
await type('soon');
out.search_disabled_typed = footButton('Search') ? footButton('Search').disabled : null;
out.dialog_text += d.querySelector('.modal-panel').textContent;
await press(searchButton());
out.too_soon = d.querySelector('.sp-msg.warn') ? d.querySelector('.sp-msg.warn').textContent : null;
await type('xyzzy');
d.getElementById('sp-entry').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
await wait();
const msgLink = d.querySelector('#sp-msg a');
out.not_found_link = msgLink ? { text: msgLink.textContent, target: msgLink.target } : null;
await type('reds');
await press(searchButton());
const league = d.querySelector('.sp-confirm-sub a');
out.league_link = league ? { text: league.textContent, target: league.target, href: league.href } : null;
const onlyTeam = d.querySelector('input[name="sp-follow"][value="team"]');
out.only_team_checked = onlyTeam ? onlyTeam.checked : null;
out.confirm_foot = foot();
out.best_match_after_search = Boolean(d.getElementById('sp-best'));
out.confirm_text = d.querySelector('.modal-panel').textContent;
await press(footButton('Back'));
out.back_value = d.getElementById('sp-entry') ? d.getElementById('sp-entry').value : null;
out.back_foot = foot();
out.back_search_enabled = footButton('Search') ? !footButton('Search').disabled : null;
if (d.getElementById('sp-entry')) {
  await type('');
  await press(d.querySelector('.sp-pick:not([disabled])'));
  out.best_match_after_pick = Boolean(d.getElementById('sp-best'));
  await press(footButton('Back'));
}
// A key that returns several rows: the "which one" list, then Escape steps back rather than closing.
const escape = async () => { d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await wait(); };
if (d.getElementById('sp-entry')) {
  await type('two');
  await press(searchButton());
  out.choices = Array.from(d.querySelectorAll('[data-choice]')).map((b) => b.textContent.trim());
  out.choices_foot = foot();
  out.choices_text = d.querySelector('.modal-panel').textContent;
  await press(d.querySelector('[data-choice="1"]'));
  out.choice_confirm_team = d.querySelector('.sp-follow-group') ? d.querySelector('.sp-follow-group').textContent : null;
  out.best_match_after_choice = Boolean(d.getElementById('sp-best'));
  await escape();
  out.escape_value = d.getElementById('sp-entry') ? d.getElementById('sp-entry').value : null;
  out.escape_dialog_open = Boolean(d.querySelector('.modal'));
}
if (d.getElementById('sp-entry')) {
  await type('reds');
  await press(searchButton());
}
await press(footButton('Follow'));
out.follow_body = requests.filter((r) => r.path === '/api/sports/leagues').map((r) => r.body);

console.log(JSON.stringify(out));
process.exit(0);
