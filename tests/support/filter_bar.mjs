/* Drives the shared filter bar (static/js/filter-bar.js) in jsdom and prints what it
   observed as JSON. Called by tests/test_filter_bar_js.py, which owns every assertion -
   this file only reports, so a failure reads as "the bar did X" in Python rather than as
   a node exit code. Same shape as tests/support/logs_page.mjs.

   Why a real DOM rather than the pure-function harness tests/test_dropdown_js.py uses:
   half of what this component promises is about the DOM it shares with util.js. That a
   value pick does NOT dismiss the popover is a fact about util.js's document-level "a
   .menu-item click closes the menu" handler being stopped, and it cannot be observed
   without both files in one window. The shipped util.js is therefore evaluated here the
   way its <script src> in base.html would have been, not stubbed.

   jsdom computes no layout, so nothing about where the popover lands, whether the chips
   wrap at 375px, or the viewport clamp can be asked here - those need a browser.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const JS = ['util.js', 'filter-bar.js'].map(f => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <div class="toolbar">
    <div class="chips" id="chips">
      <div class="menu-wrap">
        <button class="chip" id="add" type="button" data-menu>+ Filter</button>
        <div class="menu pop-left" id="menu"></div>
      </div>
    </div>
  </div>
</body></html>`;

// Four rows over three dimensions, deliberately uneven: two accounts so the counts
// differ, one row carrying a label with markup in it so escaping is observable, and a
// dimension nobody offers.
const ROWS = [
  { name: 'a', status: 'PASS', account: '1' },
  { name: 'b', status: 'PASS', account: '2' },
  { name: 'c', status: 'FAIL', account: '2' },
  { name: 'd', status: 'WARN', account: '2' },
];

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    url: 'http://localhost:5000/',
    virtualConsole: vc,
  });
  const w = dom.window;
  JS.forEach(src => w.eval(src));

  // Rows and the offered set are mutable so the pruning scenario can take a value away
  // underneath a bar that is already filtering on it.
  w.eval(`
    var ROWS = ${JSON.stringify(ROWS)};
    var accountOffered = true;
    var changes = 0;
    var DIMS = [
      { k: 'status', label: 'Status',
        values: [{v:'PASS',label:'Pass'},{v:'FAIL',label:'Fail'},{v:'WARN',label:'<b>Warn</b>'}],
        match: (r, v) => r.status === v },
      { k: 'account', label: 'Account',
        values: () => [...new Set(ROWS.map(r => r.account))].sort().map(v => ({v, label: 'Account ' + v})),
        match: (r, v) => r.account === v,
        available: () => accountOffered },
      { k: 'never', label: 'Never offered', values: [{v:'x',label:'X'}],
        match: () => true, available: () => false },
    ];
    var bar = createFilterBar({
      chipsEl: document.getElementById('chips'),
      menuEl: document.getElementById('menu'),
      dims: DIMS,
      rows: () => ROWS,
      note: 'or within, and across',
      onChange: () => { changes++; },
    });
    function chipLabels() {
      return [...document.querySelectorAll('#chips .chip.active-filter')].map(c => c.textContent.trim());
    }
    function menuRows() {
      return [...document.querySelectorAll('#menu .menu-item')].map(b => b.textContent.trim());
    }
    function visible() { return ROWS.filter(r => bar.matches(r)).map(r => r.name); }
    function open() { document.getElementById('add').click(); }
    function isOpen() { return document.getElementById('menu').classList.contains('open'); }
    function clickDim(k) { document.querySelector('[data-fdim="' + k + '"]:not([data-fval])').click(); }
    function clickVal(k, v) {
      document.querySelector('[data-fdim="' + k + '"][data-fval="' + v + '"]').click();
    }
    function clickChip(i) { document.querySelectorAll('#chips .chip.active-filter')[i].click(); }
    function snap() {
      return { chips: chipLabels(), rows: visible(), count: bar.count(),
               menu: menuRows(), open: isOpen(), entries: bar.entries() };
    }
  `);
  return { w, errors };
}

const out = {};
const { w, errors } = boot();
const run = (expr) => w.eval(expr);

// 1. Nothing is active at load, and the popover offers the dimensions - not the values.
out.initial = run('snap()');
out.initial_html = run('document.getElementById("menu").innerHTML');

// 2. Drilling in shows the values with a per-value count, and back returns to the list.
run('open(); clickDim("status")');
out.drilled = run('snap()');
out.drilled_html = run('document.getElementById("menu").innerHTML');
run('document.querySelector("[data-fback]").click()');
out.after_back = run('snap()');

// 3. One value: a chip, a filtered list, and the popover still open.
run('clickDim("status"); clickVal("status", "FAIL")');
out.one_value = run('snap()');

// 4. A second value in the same dimension ORs with the first.
run('clickVal("status", "PASS")');
out.or_within = run('snap()');

// 5. A second dimension ANDs with the first.
run('document.querySelector("[data-fback]").click(); clickDim("account"); clickVal("account", "2")');
out.and_across = run('snap()');

// 6. Clicking a chip removes exactly that filter.
run('clickChip(0)');
out.chip_removed = run('snap()');

// 7. A value that stops existing takes its chip with it rather than filtering invisibly.
run('bar.clear(); bar.toggle("account", "1"); bar.render()');
out.before_prune = run('snap()');
run('ROWS = ROWS.filter(r => r.account !== "1"); bar.render()');
out.after_prune = run('snap()');

// 8. So does a whole dimension that stops being offered.
run('ROWS = ' + JSON.stringify(ROWS) + '; bar.clear(); bar.toggle("account", "1"); bar.render()');
out.before_unoffer = run('snap()');
run('accountOffered = false; bar.render()');
out.after_unoffer = run('snap()');

// 9. setFrom / entries round-trip, which is what a URL-persisted bar rides on.
run('accountOffered = true; bar.clear(); bar.setFrom([["status","FAIL"],["account","2"],["bogus","x"]]); bar.render()');
out.restored = run('snap()');

out.changes = run('changes');
out.errors = errors;
process.stdout.write(JSON.stringify(out));
