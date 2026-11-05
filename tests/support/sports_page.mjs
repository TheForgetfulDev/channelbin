/* Drives the REAL Sports Schedules list (static/js/sports.js over the markup the app rendered) in
   jsdom and prints what it observed as JSON. Called by tests/test_sports_page_js.py, which owns every
   assertion - this file only reports. Faked: fetch (answers per request), and followHref. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const HTML = fs.readFileSync(`${DIR}/list.html`, 'utf8');
const JS = ['util.js', 'sports.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const wait = (ms = 15) => new Promise((r) => setTimeout(r, ms));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => { if (!/navigation|reload/i.test(e.message)) errors.push(e.message); });
const dom = new JSDOM(HTML, { runScripts: 'outside-only', pretendToBeVisual: true,
                              url: 'http://localhost:5000/sports', virtualConsole: vc });
const w = dom.window;
const d = w.document;
const requests = [];
const ANSWERS = {
  'POST /api/sports/lookup': (body) => {
    if (body.text === 'soon') return [409, { error: 'ChannelBin makes one call at a time to TheSportsDB, and one just went out. Try again in 8 seconds.' }];
    if (body.text === 'xyzzy') return [404, { error: 'TheSportsDB found no team called "xyzzy". A league with no teams, such as a racing series, can be added with its number or a link to its page on TheSportsDB.' }];
    return [200, { success: true, league: { tsdb_id: 9001, name: 'Pretend League', sport: 'Pretend Ball', season: '2099' },
                   team: body.text === 'reds' ? { team_id: 11, team: 'Pretendville Reds' } : undefined }];
  },
  'POST /api/sports/leagues': () => [200, { success: true, id: 1 }],
};
w.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const path = String(url).replace('http://localhost:5000', '');
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push({ method, path, body });
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
w.eval('closeMenus()');

await click(d.getElementById('sp-add'));
const hint = d.querySelector('.modal-body .form-hint a');
out.hint_link = hint ? { text: hint.textContent, target: hint.target, rel: hint.rel } : null;
out.picks = d.querySelectorAll('.sp-pick').length;
const type = async (text) => {
  const input = d.getElementById('sp-entry');
  input.value = text;
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  await wait(2);
};
await type('motorsport');
out.filtered_picks = d.querySelectorAll('.sp-pick').length;
out.calls_while_typing = requests.length;
await type('soon');
await click(d.querySelector('[data-lookup]'));
out.too_soon = d.querySelector('.sp-msg.warn') ? d.querySelector('.sp-msg.warn').textContent : null;
await type('xyzzy');
await click(d.querySelector('[data-lookup]'));
const msgLink = d.querySelector('#sp-msg a');
out.not_found_link = msgLink ? { text: msgLink.textContent, target: msgLink.target } : null;
await type('reds');
await click(d.querySelector('[data-lookup]'));
const league = d.querySelector('.sp-confirm-sub a');
out.league_link = league ? { text: league.textContent, target: league.target, href: league.href } : null;
out.only_team_checked = d.querySelector('input[name="sp-follow"][value="team"]').checked;
await click(Array.from(d.querySelectorAll('.modal-foot button')).find((b) => b.textContent === 'Follow'));
out.follow_body = requests.filter((r) => r.path === '/api/sports/leagues').map((r) => r.body);

console.log(JSON.stringify(out));
process.exit(0);
