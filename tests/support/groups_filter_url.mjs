/* Drives the REAL /channel-groups page in jsdom to watch its filter bar ride the URL, and
   prints what it observed as JSON. Called by tests/test_groups_filter_url_js.py, which owns
   every assertion - this file only reports. Same shape as tests/support/health_check_pages.mjs.

   The Groups tab is the one list page that persists the shared bar's chips to the query
   string: `f=<k>:<v>` for "is" and `x=<k>:<v>` for "is not" (dev/changelog/1207). Neither half
   is reachable from Python - the server renders the same empty chip row whatever the URL.

   argv: <fixture dir> <repo root>. The fixture dir holds groups.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/groups.html`, 'utf8');
const JS = ['util.js', 'format-plan.js', 'create-group-modal.js', 'clone-modal.js',
  'group-delete.js', 'filter-bar.js', 'groups.js'];

function boot(query) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => { if (!/navigation/i.test(e.message)) errors.push(`jsdomError: ${e.message}`); });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: `http://localhost:5000/channel-groups${query}`,
    virtualConsole: vc,
    beforeParse(w) {
      w.fetch = () => Promise.resolve({
        ok: true, status: 200, headers: { get: () => 'application/json' },
        json: () => Promise.resolve({ success: true }), text: () => Promise.resolve('{}'),
      });
      w.matchMedia = (q) => ({
        media: q, matches: false, onchange: null, addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
    },
  });
  const { window } = dom;
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  JS.forEach((f) => window.eval(fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8')));
  const d = window.document;
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  const snap = () => ({
    shown: [...d.querySelectorAll('.grp-item')].filter((r) => r.style.display !== 'none')
      .map((r) => r.dataset.name),
    chips: [...d.querySelectorAll('#filter-chips .chip.active-filter')]
      .map((c) => c.textContent.replace(/\s+/g, ' ').trim()),
    search: decodeURIComponent(window.location.search),
    errors,
  });
  return { window, d, click, snap };
}

const out = {};
try {
  // 1. A URL from before "is not" existed: the f= half alone.
  out.is_url = boot('?f=guide:in').snap();
  // 2. The x= half reads as "is not".
  out.not_url = boot('?x=guide:in').snap();
  // 3. Choosing "is not" in the popover writes x=, and removing its chip takes it away.
  const p = boot('');
  p.click(p.d.querySelector('#filter-chips [data-menu]'));
  p.click(p.d.querySelector('#filter-menu [data-fdim="guide"]:not([data-fval])'));
  p.click(p.d.querySelector('#filter-menu [data-fdim="guide"][data-fval="in"][data-fop="not"]'));
  out.picked = p.snap();
  p.click(p.d.querySelector('#filter-chips .chip.active-filter'));
  out.removed = p.snap();
} catch (e) {
  out.threw = `threw: ${e.message}`;
}
process.stdout.write(JSON.stringify(out), () => process.exit(0));
