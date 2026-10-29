/* Drives the REAL group page's What's On card in jsdom: the page /channel-groups/<id>
   rendered, the shipped util.js + guide.js + guide-schedules.js, and the payload the real
   /api/guide/epg?group_id=...&schedules=1 returned. Reports what the card drew after each of
   its controls, as JSON. Called by tests/test_group_whats_on.py, which owns every assertion
   (dev/changelog/1324).

   argv: <fixture dir> <repo root> <width>. The fixture dir holds page.html and epg.json. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO, WIDTH] = process.argv;
const PAGE = fs.readFileSync(`${DIR}/page.html`, 'utf8');
const EPG = JSON.parse(fs.readFileSync(`${DIR}/epg.json`, 'utf8'));
const PHONE = Number(WIDTH) <= 960;

const JS = ['util.js', 'guide.js', 'guide-schedules.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const errors = [];
const requests = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

const fetchStub = (target, opts) => {
  requests.push({ url: String(target), method: (opts && opts.method) || 'GET' });
  const payload = String(target).includes('/api/guide/epg') ? EPG : { success: true, results: [] };
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: () => Promise.resolve(payload),
    text: () => Promise.resolve(JSON.stringify(payload)),
  });
};

// Only the page's inline scripts run from the markup: its <script src> tags are not loaded,
// so the three files under test are evaluated by hand below.
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://localhost:5000/channel-groups/1',
  virtualConsole: vc,
  beforeParse(w) {
    w.fetch = fetchStub;
    Object.defineProperty(w, 'innerWidth', { value: Number(WIDTH), configurable: true });
    w.matchMedia = (query) => ({
      media: query, matches: PHONE, onchange: null,
      addEventListener() {}, removeEventListener() {},
      addListener() {}, removeListener() {}, dispatchEvent() { return false; },
    });
  },
});
const { window } = dom;
const doc = window.document;
window.scrollTo = () => {};
window.HTMLElement.prototype.scrollIntoView = () => {};
// jsdom fires DOMContentLoaded after this constructor returns, so the scripts evaluated
// here register for it and boot exactly as they do in a browser.
window.eval(JS.join('\n'));

const tick = () => new Promise((r) => setTimeout(r, 0));

function snapshot() {
  const rows = Array.from(doc.querySelectorAll('#guide-program-rows .guide-row'));
  const col = Array.from(doc.querySelectorAll('#guide-channel-col .guide-channel-row'));
  const kinds = {};
  doc.querySelectorAll('#guide-program-rows .guide-program').forEach((el) => {
    const k = Array.from(el.classList).find((c) => c.startsWith('sched-k-'));
    const key = k || (el.classList.contains('sched-ghost') ? 'ghost' : 'none');
    kinds[key] = (kinds[key] || 0) + 1;
  });
  return {
    row_ids: rows.map((r) => r.dataset.channelId),
    col_keys: col.map((c) => c.dataset.sched),
    col_badges: col.map((c) => {
      const b = c.querySelector('.badge');
      return b ? b.textContent.trim() : null;
    }),
    col_rows_match: rows.length === col.length,
    kinds,
    tinted: doc.querySelectorAll('.guide-program.sched-tinted').length,
    gap_marks: Array.from(doc.querySelectorAll('.sched-gapmark')).map((m) => m.className),
    ghosts: doc.querySelectorAll('.sched-ghost').length,
    bands: Array.from(doc.querySelectorAll('.sched-band')).map((b) => b.textContent.trim()),
    descriptions: doc.querySelectorAll('#guide-program-rows .guide-program-desc').length,
    chips: Array.from(doc.querySelectorAll('#sched-card .chip.active')).map((c) => c.textContent.trim()),
    counts: (doc.getElementById('sched-counts') || {}).textContent || '',
    summary: (doc.getElementById('sched-summary') || {}).textContent || '',
    empty: doc.getElementById('sched-empty').hidden ? '' : doc.getElementById('sched-empty').textContent,
    noid: doc.getElementById('sched-noid').hidden ? '' : doc.getElementById('sched-noid').textContent,
    fold: (doc.querySelector('.sched-fold') || {}).textContent || '',
    legend: Array.from(doc.querySelectorAll('#sched-legend .guide-legend-item')).map((l) => l.textContent.trim()),
    rowh: doc.documentElement.style.getPropertyValue('--guide-rowh'),
    cell_tip: (doc.querySelector('#guide-program-rows .guide-row:nth-child(2) .guide-program') || { dataset: {} }).dataset.tip || '',
  };
}

const click = (sel) => doc.querySelector(sel).dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

(async () => {
  for (let i = 0; i < 5; i += 1) await tick();
  const out = { errors, phone: PHONE };
  out.boot = snapshot();

  click('[data-chan="rec"]');
  out.rec = snapshot();
  click('[data-chan="all"]');

  click('[data-show="same"]');
  out.same = snapshot();
  click('[data-show="different"]');
  out.different = snapshot();
  click('[data-show="all"]');

  // The fold is a Layout checkbox: guide.js's own popover wiring saves and re-renders it.
  const fold = doc.querySelector('#guide-layout-pop [data-layout="fold_same"]');
  fold.checked = true;
  fold.dispatchEvent(new window.Event('change', { bubbles: true }));
  out.folded = snapshot();
  out.fold_saved = requests.filter((r) => r.method === 'POST').map((r) => r.url);
  fold.checked = false;
  fold.dispatchEvent(new window.Event('change', { bubbles: true }));

  // A schedule cell in the channel column opens its details.
  click('#guide-channel-col .guide-channel-row[data-sched="s1"]');
  const modal = doc.querySelector('.guide-sheet .modal-body');
  out.details = modal ? modal.textContent.replace(/\s+/g, ' ').trim() : null;
  out.epg_requests = requests.filter((r) => r.url.includes('/api/guide/epg')).map((r) => r.url);
  console.log(JSON.stringify(out));
  process.exit(0);
})();
