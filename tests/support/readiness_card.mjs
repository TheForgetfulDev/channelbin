/* Drives the REAL Readiness card on Maintenance in jsdom and prints what it observed as
   JSON. Called by tests/test_readiness_card_js.py, which owns every assertion - this file
   only reports. Same shape as tests/support/maintenance_restart.mjs.

   What is real: the markup the /maintenance route rendered, the payload readiness.evaluate()
   built on a seeded install, and util.js + readiness.js evaluated as shipped. Faked: the
   network (every /api/readiness answer is that payload).

   argv: <fixture dir> <repo root>. The fixture dir holds page.html and payload.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const html = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const payload = JSON.parse(fs.readFileSync(`${DIR}/payload.json`, 'utf8'));
const JS = ['util.js', 'readiness.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const respond = (body) => Promise.resolve({
  ok: true,
  status: 200,
  headers: { get: () => 'application/json' },
  json: () => Promise.resolve(body),
  text: () => Promise.resolve(JSON.stringify(body)),
});

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/maintenance',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (target) => respond(String(target).includes('/api/readiness') ? payload : {});
  },
});
const { window } = dom;
const { document } = window;
window.scrollTo = () => {};
JS.forEach((src) => window.eval(src));

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (s) => document.querySelector(s);
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);

await wait(20);
const capItem = () => $('.rd-caprow[data-cap="failover"]').closest('.grp-item');
const out = {
  capName: text(capItem().querySelector('.rd-capname')),
  capWhy: text(capItem().querySelector('.rd-capwhy')),
  capAction: text(capItem().querySelector('.rd-capact a')),
  runAllTip: $('#rd-runall').getAttribute('data-tip'),
  runAllLabel: text($('#rd-runall')),
  meta: text($('.rd-vmeta')),
  verdictHead: text($('.rd-vhead')),
  pill: text($('#rd-head-pill')),
};
click($('.rd-caprow[data-cap="failover"]'));
await wait(5);
click(capItem().querySelector('[data-rowtoggle="failover:guide_groups"]'));
await wait(5);
const row = capItem().querySelector('[data-row="failover:guide_groups"]');
out.rowAction = text(row.querySelector('.rd-ract a'));
out.rowDetail = text(row.querySelector('.rd-detail'));
const tested = capItem().querySelector('[data-row="failover:channels_tested"]');
out.testedLink = tested ? !!tested.querySelector('.rd-ract a, .rd-ract [data-fix]') : null;
// The same card over a payload that is degraded and nothing blocked, for the head pill.
const degraded = { ...payload, nav: { ...payload.nav, blocked: 0, degraded: 1 } };
const dom2 = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/maintenance',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (target) => respond(String(target).includes('/api/readiness') ? degraded : {});
  },
});
dom2.window.scrollTo = () => {};
JS.forEach((src) => dom2.window.eval(src));
await wait(20);
out.degradedPill = text(dom2.window.document.querySelector('#rd-head-pill'));
out.errors = errors;
process.stdout.write(JSON.stringify(out));
process.exit(0);
