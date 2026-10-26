/* Loads a page the real app rendered, with base.html's own inline scripts running as shipped,
   and reports what the menu's badges and the footer read before any /api/nav-status answer
   arrives. Called by tests/test_nav_first_paint.py, which owns every assertion - this file
   only reports.

   The external scripts are not fetched by jsdom, so util.js and nav-alerts.js are evaluated
   here right after the parse - before DOMContentLoaded, which is where base.html reads its
   boot block. The poll's fetch never answers, so everything observed came from that block.

   argv: <repo root> <rendered page html path>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, PAGE_PATH] = process.argv;
const html = fs.readFileSync(PAGE_PATH, 'utf8');
const js = (f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8');

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = () => new Promise(() => {});
    w.setInterval = () => 0;
  },
});
const w = dom.window;
w.scrollTo = () => {};
w.eval(js('util.js'));
w.eval(js('nav-alerts.js'));
await new Promise((r) => setTimeout(r, 20));

const d = w.document;
const badge = (sel) => {
  const el = d.querySelector(`.sidebar ${sel}`);
  return el ? {
    text: el.textContent.trim(),
    shown: el.style.display !== 'none',
    tip: el.getAttribute('data-tip'),
  } : null;
};
const ss = (key) => {
  const el = d.querySelector(`.sidebar [data-ss="${key}"]`);
  return el ? el.textContent.trim() : null;
};

process.stdout.write(JSON.stringify({
  errors,
  readyBad: badge('.nav-count-ready-bad'),
  readyWarn: badge('.nav-count-ready-warn'),
  alertBad: badge('.nav-count-bad'),
  alertWarn: badge('.nav-count-warn'),
  recordings: badge('.nav-count-recordings'),
  cpu: ss('cpu'),
  disk: ss('disk'),
}));
process.exit(0);
