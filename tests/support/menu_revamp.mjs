/* Drives the main menu's folding sections and the Recordings header's Schedule recording
   button in jsdom, and prints what it observed as JSON. Called by tests/test_menu_revamp_js.py,
   which owns every assertion - this file only reports.

   What is real: the page is what the Flask app rendered; base.html's inline scripts, util.js,
   nav-alerts.js and recordings.js are the shipped files, inlined where their <script src>
   tags sit, so they run in the order a browser runs them. Faked: the network (a fetch that
   records what was sent and never answers, so every badge comes from the page's own boot
   block) and, in phone mode, the mobile media query.

   argv: <repo root> <rendered page html path> <mode: folds | schedule> <width: desktop | phone>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, PAGE_PATH, MODE, WIDTH] = process.argv;

// duplicated from tests/support/recordings_page.mjs - each harness is run standalone by its
// own test module, and a shared import would couple two fixtures' lifetimes.
const inlineScripts = (html) => html.replace(
  /<script src="[^"]*\/static\/js\/([\w.-]+\.js)[^"]*"><\/script>/g,
  (tag, name) => {
    const path = `${REPO}/static/js/${name}`;
    return fs.existsSync(path) ? `<script>${fs.readFileSync(path, 'utf8')}</script>` : '';
  });

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const sent = [];
const dom = new JSDOM(inlineScripts(fs.readFileSync(PAGE_PATH, 'utf8')), {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = (url, opts = {}) => {
      if (opts.method === 'POST') sent.push({ url: String(url), body: JSON.parse(opts.body || 'null') });
      return new Promise(() => {});
    };
    w.setInterval = () => 0;
    w.scrollTo = () => {};
    const phone = WIDTH === 'phone';
    w.matchMedia = (q) => ({ media: q, matches: phone && q.includes('max-width: 960px'),
      addEventListener() {}, removeEventListener() {} });
  },
});
const w = dom.window;
const d = w.document;
await new Promise((r) => setTimeout(r, 20));

const sections = () => ['.sidebar', '.topnav-menu'].map((shell) => [...d.querySelectorAll(`${shell} .nav-sec[data-fold]`)].map((s) => ({
  key: s.dataset.fold,
  folded: s.classList.contains('is-folded'),
  expanded: s.querySelector('.nav-fold').getAttribute('aria-expanded'),
  sum: [...s.querySelectorAll('.nav-sum .nav-count')].map((c) => ({
    cls: c.className, text: c.textContent.trim(), tip: c.getAttribute('data-tip'),
  })),
})));

const out = { errors, sent };
if (MODE === 'folds') {
  out.atLoad = sections();
  out.sentAtLoad = sent.length;
  // A fold button in the phone menu: both copies of the section follow it.
  const sys = d.querySelector('.topnav-menu .nav-sec[data-fold="system"] .nav-fold');
  if (sys) sys.click();
  out.afterToggle = sections();
} else {
  const btns = [...d.querySelectorAll('[data-sched-menu]')];
  out.buttons = btns.length;
  btns[0].click();
  out.menuOpen = !!d.querySelector('.menu.open');
  out.sheet = [...d.querySelectorAll('.modal .act-tile')].map((a) => ({
    href: a.getAttribute('href'), label: a.textContent.trim(),
    icon: (a.querySelector('.icon') || { dataset: {} }).dataset.icon,
  }));
  out.sheetTitle = (d.querySelector('.modal .modal-head') || { textContent: '' }).textContent.trim();
  out.sheetNote = (d.querySelector('.modal .act-tiles-note') || { textContent: '' }).textContent.trim();
}
process.stdout.write(JSON.stringify(out));
process.exit(0);
