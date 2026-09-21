/* Drives the REAL Dashboard timeline's clock in jsdom and prints what it observed as JSON.
   Called by tests/test_dashboard_timeline_tick.py, which owns every assertion - this file
   only takes the measurements. Same shape as tests/support/dashboard_live.mjs.

   Why it exists: whether the axis keeps time is decided entirely in dashboard.js, from one
   number (TL.data.now) that no Python test can see and that nothing used to reassign
   (dev/changelog/1066). The positions it produces are style attributes written by
   renderTimeline, so they ARE readable without layout - which is what makes this checkable
   in jsdom at all. What a browser still owes is the part jsdom has no layout for: that the
   scroll offset survives the rebuild and nothing jumps.

   What is real: the markup the Flask dashboard route rendered for a seeded live recording,
   and util.js + dashboard.js evaluated as shipped. Faked: EventSource, fetch, matchMedia
   (desktop), the browser clock, setInterval (captured rather than run), and document.hidden.

   argv: <fixture dir> <repo root>. The fixture dir holds page.html. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const JS = ['util.js', 'dashboard.js'].map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const MINUTE = 60000;

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

// One controllable clock. dashboard.js reads it through Date.now() for the elapsed time
// it adds to the server's reading, so moving this is how the test moves "now".
let clock = Date.now();
const intervals = [];
let hidden = false;

const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => 'application/json' },
      json: () => Promise.resolve({ active: false }),
      text: () => Promise.resolve('{}'),
    });
    w.EventSource = class { constructor(url) { this.url = url; } close() {} };
    // Desktop, so the scale is PX_PER_MIN_DESKTOP and the assertions can name one number.
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.Date.now = () => clock;
    // Captured, never run: the page's own 60s tick is invoked by hand below, so the
    // observation is of one tick and not of however many a real timer happened to fire.
    w.setInterval = (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; };
    w.scrollTo = () => {};
  },
});
const { window } = dom;
const { document } = window;

Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });

// As <script> elements rather than eval: dashboard.js reads util.js's top-level `const`s,
// which only the shared global lexical scope of real scripts makes visible.
JS.forEach((src) => {
  const el = document.createElement('script');
  el.textContent = src;
  document.body.appendChild(el);
});
document.dispatchEvent(new window.Event('DOMContentLoaded'));

const px = (el, prop) => (el ? parseFloat(el.style[prop]) : null);
const all = (sel, prop) => Array.from(document.querySelectorAll(sel)).map((el) => px(el, prop));
// The hour an .tl-tick names. The window is anchored to `now`, so advancing the clock
// drops hours off one end and adds them at the other - two snapshots' tick lists are the
// same length only by luck, and position N is not the same hour in both. The label is what
// identifies a tick across a snapshot.
const allText = (sel) => Array.from(document.querySelectorAll(sel)).map((el) => el.textContent);

/* Every position renderTimeline writes, as numbers. `nowLeft` is deliberately in here:
   TL.start is always now - TL_BACK_HOURS, so tlPx(now) is a CONSTANT and the now-line is a
   fixed post that the drawing slides underneath. A test that expected it to move would be
   asserting the opposite of the design. */
const snapshot = () => ({
  nowLeft: px(document.querySelector('#tl .tl-now'), 'left'),
  trackWidth: px(document.querySelector('#tl .tl-track'), 'width'),
  tickLefts: all('#tl .tl-tick', 'left'),
  tickLabels: allText('#tl .tl-tick'),
  gridLefts: all('#tl .tl-grid i', 'left'),
  barLefts: all('#tl .tl-bar', 'left'),
  fillWidths: all('#tl .tl-bar.live .tl-fill', 'width'),
  bars: document.querySelectorAll('#tl .tl-bar').length,
  counts: (document.querySelector('#tl .tl-off') || {}).textContent || null,
  scrollerPresent: !!document.getElementById('tl-scroll'),
  tips: Array.from(document.querySelectorAll('#tl [data-tip]')).map((el) => el.getAttribute('data-tip')),
});

// The page's own tick, found by its period rather than by call order, so an unrelated
// setInterval elsewhere in the file cannot be mistaken for it.
const ticks = intervals.filter((i) => i.ms === MINUTE);
const fire = () => ticks.forEach((t) => t.fn());

const out = { intervalPeriods: intervals.map((i) => i.ms), ticksFound: ticks.length };
out.initial = snapshot();

// One minute of wall clock with the tab in front of the user.
clock += MINUTE;
fire();
out.afterOneMinute = snapshot();

// Ten more with the tab hidden: the tick must do nothing at all.
hidden = true;
for (let i = 0; i < 10; i += 1) { clock += MINUTE; fire(); }
out.afterHiddenTicks = snapshot();

// ...and the render it owes lands the moment the tab is looked at again, without
// waiting out the rest of the period.
hidden = false;
document.dispatchEvent(new window.Event('visibilitychange'));
out.afterBecomingVisible = snapshot();

out.errors = errors;
process.stdout.write(JSON.stringify(out));
process.exit(0);
