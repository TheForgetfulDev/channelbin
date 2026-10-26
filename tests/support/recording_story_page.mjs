/* Drives two REAL pages in jsdom and prints what it observed as JSON. Called by
   tests/test_recording_story.py, which owns every assertion - this file only reports.

   Why it exists (dev/changelog/1270): two halves of "trouble is told plainly where people
   look" live entirely in the browser. On the Recordings list a tap on the health pill - the
   one place the list summarizes what went wrong - did nothing, because the row's navigation
   skipped every tooltip and a tap opens no tooltip. On the recording page the "why" a reader
   had opened on a stalled segment closed whenever the page reloaded, which the live page
   does on every status change.

   What is real: the markup the Flask routes rendered, and the shipped util.js, filter-bar.js,
   guide.js and recordings.js inlined where their <script src> tags sit. Faked: the network
   (empty answers), EventSource, matchMedia, and navigation, which jsdom reports as
   unimplemented - that report is how a navigation is counted.

   argv: <fixture dir> <repo root>. The fixture dir holds list.html, detail.html and
   ids.json ({list_row, detail_seg}). */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , DIR, REPO] = process.argv;
const read = (f) => fs.readFileSync(`${DIR}/${f}`, 'utf8');
const IDS = JSON.parse(read('ids.json'));

// duplicated from tests/support/recordings_page.mjs - each harness is run standalone by its
// own test module, and a shared import would couple two fixtures' lifetimes.
const inlineScripts = (html) => html.replace(
  /<script src="[^"]*\/static\/js\/([\w.-]+\.js)[^"]*"><\/script>/g,
  (tag, name) => {
    const path = `${REPO}/static/js/${name}`;
    return fs.existsSync(path) ? `<script>${fs.readFileSync(path, 'utf8')}</script>` : '';
  });

function boot(html, url, storage = {}) {
  const errors = [];
  const navigations = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(inlineScripts(html), {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url,
    virtualConsole: vc,
    beforeParse(w) {
      for (const [k, v] of Object.entries(storage)) w.sessionStorage.setItem(k, v);
      w.fetch = () => Promise.resolve({
        ok: true, status: 200, headers: { get: () => 'application/json' },
        json: () => Promise.resolve({}), text: () => Promise.resolve('{}'),
      });
      w.EventSource = class { constructor(u) { this.url = u; } close() {} };
      w.matchMedia = () => ({
        media: '', matches: false, onchange: null,
        addEventListener() {}, removeEventListener() {},
        addListener() {}, removeListener() {}, dispatchEvent() { return false; },
      });
      w.scrollTo = () => {};
    },
  });
  return { window: dom.window, errors, navigations };
}

const click = (w, el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));

// ── The list: does a tap on the health pill reach the recording? ──
const list = boot(read('list.html'), 'http://localhost:5000/recordings');
const row = list.window.document.querySelector(`.rec-list .rows .row[data-id="${IDS.list_row}"]`);
const pill = row && row.querySelector('.h-mini');
const pillNavs = (() => {
  const before = list.navigations.length;
  if (pill) click(list.window, pill);
  return list.navigations.length - before;
})();
// Any other tooltip on the row still keeps the click for itself, as before.
const otherTip = row && Array.from(row.querySelectorAll('[data-tip]')).find((el) => !el.classList.contains('h-mini'));
const otherNavs = (() => {
  const before = list.navigations.length;
  if (otherTip) click(list.window, otherTip);
  return list.navigations.length - before;
})();

// ── The recording page: does an opened "why" survive a reload? ──
const detailUrl = `http://localhost:5000/recordings/${IDS.detail_rec}`;
const first = boot(read('detail.html'), detailUrl);
const why = first.window.document.querySelector(`.seg-why[data-seg-no="${IDS.detail_seg}"]`);
if (why) {
  why.open = true;
  why.dispatchEvent(new first.window.Event('toggle'));
}
const stored = {};
for (let i = 0; i < first.window.sessionStorage.length; i += 1) {
  const k = first.window.sessionStorage.key(i);
  stored[k] = first.window.sessionStorage.getItem(k);
}
const second = boot(read('detail.html'), detailUrl, stored);
const whyAfter = second.window.document.querySelector(`.seg-why[data-seg-no="${IDS.detail_seg}"]`);

process.stdout.write(JSON.stringify({
  list: { errors: list.errors, found_pill: !!pill, pill_navigations: pillNavs,
          found_other_tip: !!otherTip, other_tip_navigations: otherNavs },
  detail: { errors: first.errors.concat(second.errors), found_why: !!why,
            stored, open_after_reload: whyAfter ? whyAfter.open : null },
}));
process.exit(0);
