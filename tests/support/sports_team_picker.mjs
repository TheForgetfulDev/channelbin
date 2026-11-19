/* Drives the REAL team picker (static/js/sports.js over the Sports Schedules list the app rendered)
   in jsdom and prints what it observed as JSON: Add league or team's confirm step for a league here
   with games, one ChannelBin has never had, and Change teams for a league followed by team before
   its games. Called by tests/test_sports_team_picker_js.py, which owns every assertion - this file
   only reports. Faked: fetch (the answers come from `answers.json`, which the test built from the
   app's own lookup route for a league already here) and layout (jsdom_layout.mjs). Timers are real,
   so the one-call-at-a-time wait is sat out. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';
import { fakeLayout } from './jsdom_layout.mjs';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/list.html`, 'utf8');
const A = JSON.parse(fs.readFileSync(`${DIR}/answers.json`, 'utf8'));
const JS = ['util.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/navigation|reload/i.test(e.message)) errors.push(e.message); });
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:5000/sports', virtualConsole: vc });
const w = dom.window;
const d = w.document;
fakeLayout(w);
w.scrollTo = () => {};
const requests = [];
const EPL = { tsdb_id: 4328, name: 'English Premier League', sport: 'Soccer', season: '2026-2027' };
const team = (id, name, league) => ({ team_id: id, team: name, league });
let lateAsked = 0;
const lookup = (body) => {
  if (body.tsdb_id === A.nfl.league.tsdb_id) return [200, A.nfl];
  if (body.text === 'reds') return [200, A.reds];
  if (body.text === 'greys') return [200, { ...A.reds, team: team(13, 'Pretendville Greys', A.nfl.league) }];
  if (body.text === 'arsenal') return [200, { success: true, league: EPL, team: team(101, 'Arsenal', EPL), best_match: true, skipped: [], here: null }];
  if (body.text === 'chelsea') return [200, { success: true, league: EPL, team: team(102, 'Chelsea', EPL), best_match: true, skipped: [], here: null }];
  if (body.text === 'owls') {
    const other = { tsdb_id: 9999, name: 'Other League', sport: 'Pretend Ball', season: null };
    return [200, { success: true, league: other, team: team(103, 'Elsewhere Owls', other), best_match: true, skipped: [], here: null }];
  }
  if (body.text === 'late') {
    lateAsked += 1;
    if (lateAsked === 1) return [409, { error: 'ChannelBin makes one call at a time to TheSportsDB, and one just went out. Try again in 1 seconds.', retry_after: 1 }];
    return [200, { success: true, league: EPL, team: team(104, 'Liverpool', EPL), best_match: true, skipped: [], here: null }];
  }
  if (body.text === 'greens') return [200, { success: true, league: A.early_league, team: team(78, 'Pretendville Greens', A.early_league), best_match: true, skipped: [], here: null }];
  return [404, { error: 'TheSportsDB found no team called that.' }];
};
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
  let answer = [200, { success: true, cut: 0 }];
  if (method === 'POST' && path === '/api/sports/lookup') answer = lookup(body);
  if (method === 'POST' && path === '/api/sports/leagues') answer = [200, { success: true, id: 5, cut: 0 }];
  const [status, payload] = answer;
  return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(payload), text: () => Promise.resolve(HTML) });
};
w.eval(JS[0]);
w.eval(JS[1]);

const click = async (el, ms) => { el.dispatchEvent(new w.MouseEvent('click', { bubbles: true })); await wait(ms); };
const press = async (el, ms) => { if (el) await click(el, ms); };
const modal = () => Array.from(d.querySelectorAll('.modal')).pop();
const q = (sel) => (modal() ? modal().querySelector(sel) : null);
const footButton = (label) => Array.from(modal().querySelectorAll('.modal-foot button')).find((b) => b.textContent.trim() === label) || null;
const text = (sel) => (q(sel) ? q(sel).textContent.trim() : null);
const effect = () => ({ text: text('#sp-effect'), warn: q('#sp-effect') ? q('#sp-effect').classList.contains('notice-warn') : null });
const radio = (v) => q(`input[name="sp-t"][value="${v}"]`);
const choose = async (v) => { const r = radio(v); if (r) { r.checked = true; r.dispatchEvent(new w.Event('change', { bubbles: true })); await wait(); } };
const tick = async (name) => {
  const row = Array.from(modal().querySelectorAll('.check-row')).find((l) => l.textContent.trim() === name);
  if (!row) return;
  const box = row.querySelector('input');
  box.checked = !box.checked;
  box.dispatchEvent(new w.Event('change', { bubbles: true }));
  await wait();
};
const chips = () => Array.from(modal().querySelectorAll('#sp-chips .sp-term')).map((c) => c.textContent.trim());
const addTeam = async (name, ms) => {
  const form = q('#sp-search');
  if (!form) return;
  form.querySelector('input').value = name;
  form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await wait(ms);
};
/* The picker draws its effect line, and enables or disables the dialog's button, on a timer after it
   is built. A fixed wait loses that race on a busy box, so wait for the line itself. */
const settled = async () => {
  for (let i = 0; i < 200 && !(q('#sp-effect') && q('#sp-effect').textContent.trim()); i++) await wait(10);
};
const typeAndSearch = async (value) => {
  const input = d.getElementById('sp-entry');
  input.value = value;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  await press(footButton('Search'));
  await settled();
};
const closeAll = async () => { Array.from(d.querySelectorAll('.modal')).forEach((m) => m.closeModal && m.closeModal()); await wait(); };
const posts = () => requests.filter((r) => r.method !== 'GET' && r.path !== '/api/sports/lookup').map((r) => ({ method: r.method, path: r.path, body: r.body }));
const out = { errors };

/* The Add list: a league followed is offered again, and says what is followed. */
await click(d.getElementById('sp-add'));
out.picks = Array.from(d.querySelectorAll('.sp-pick')).map((p) => ({ text: Array.from(p.querySelectorAll('span')).map((x) => x.textContent.trim()).join(' '), disabled: p.disabled }));

/* A team in a league followed whole, with its games here: a ticked list. */
await typeAndSearch('reds');
out.whole = {
  now: text('#sp-now'), all_checked: radio('all') ? radio('all').checked : null, effect: effect(),
  save: footButton('Save') ? footButton('Save').disabled : 'missing', follow: Boolean(footButton('Follow')),
  rows: modal().querySelectorAll('.check-row').length, best: text('#sp-best'),
};
await choose('teams');
out.narrowed = { effect: effect(), save: footButton('Save') ? footButton('Save').disabled : 'missing' };
await tick('Pretendville Blues');
out.two = effect();
const lookups = requests.filter((r) => r.path === '/api/sports/lookup').length;
await press(footButton('Save'), 30);
out.whole_post = posts().pop() || null;
out.ticking_lookups = requests.filter((r) => r.path === '/api/sports/lookup').length - lookups;
out.whole_toast = JSON.parse(w.sessionStorage.getItem('app-pending-toast') || 'null');
await closeAll();

/* A team found with no upcoming game still gets a row, so it can be unticked. */
await click(d.getElementById('sp-add'));
await typeAndSearch('greys');
await choose('teams');
out.no_game_row = Array.from(modal().querySelectorAll('.check-row')).map((l) => ({ name: l.textContent.trim(), checked: l.querySelector('input').checked }));
await closeAll();

/* A league never here: teams by search, the spacing waited out. */
await click(d.getElementById('sp-add'));
await typeAndSearch('arsenal');
out.fresh = { now: text('#sp-now'), chips: chips(), teams_checked: radio('teams') ? radio('teams').checked : null,
  follow: footButton('Follow') ? footButton('Follow').disabled : 'missing', text: modal().textContent };
await addTeam('chelsea');
out.after_chelsea = { chips: chips(), msg: text('#sp-search-msg'), effect: effect() };
await addTeam('chelsea');
out.again = text('#sp-search-msg');
await addTeam('owls');
out.other_league = { msg: text('#sp-search-msg'), chips: chips() };
await addTeam('late', 60);
out.waiting = { msg: text('#sp-search-msg'), held: q('#sp-search input') ? q('#sp-search input').disabled : null };
// The wait is a real one-second interval; poll for its result rather than guessing how long a busy box takes.
for (let i = 0; i < 100 && !chips().includes('Liverpool'); i++) await wait(50);
await wait(30);
out.waited = { chips: chips(), held: q('#sp-search input') ? q('#sp-search input').disabled : null };
const x = Array.from(modal().querySelectorAll('#sp-chips .chip-x')).find((b) => /Chelsea/.test(b.getAttribute('aria-label')));
await press(x);
out.removed = chips();
await press(footButton('Follow'), 30);
out.fresh_post = posts().pop() || null;
await closeAll();

/* Change teams for a league followed by team before its games: chips, and a search to add. */
const early = d.querySelector(`#sp-rows .row[data-league="${A.early_id}"]`);
out.early_menu = early ? Boolean(early.querySelector('[data-act="teams"]')) : null;
if (early) {
  await click(early.querySelector('.c-actions button'));
  await click(early.querySelector('[data-act="teams"]'));
  await settled();
  out.change = { title: modal().querySelector('h2').textContent, chips: chips(), now: text('#sp-now'),
    effect: effect(), save: footButton('Save') ? footButton('Save').disabled : 'missing' };
  await addTeam('greens');
  out.change_added = { chips: chips(), effect: effect(), save: footButton('Save') ? footButton('Save').disabled : 'missing' };
  await press(footButton('Save'), 30);
  out.change_post = posts().pop() || null;
}

console.log(JSON.stringify(out));
process.exit(0);
