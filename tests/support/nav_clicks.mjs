/* Drives util.js's click-navigation helpers (bindNavClicks / followHref) in jsdom and prints
   what each click did as JSON. Called by tests/test_nav_clicks_js.py, which owns every
   assertion - this file only reports.

   jsdom implements no navigation, so a real `location.href = url` is observed the one way it
   can be: jsdom reports "Not implemented: navigation" through the virtual console. A new tab
   is observed by replacing window.open, which is the call the helper makes for one.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <div id="list">
    <div class="row" data-href="/recordings/7"><span id="cell">Title</span><button id="act">Act</button></div>
  </div>
</body></html>`;

function run() {
  const navigations = [];
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => {
    if (/navigation/i.test(e.message)) navigations.push(e.message);
    else errors.push(`jsdomError: ${e.message}`);
  });
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const dom = new JSDOM(PAGE, {
    runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:5000/', virtualConsole: vc,
  });
  const w = dom.window;
  w.eval(UTIL);
  const opened = [];
  w.open = (url, target, features) => { opened.push({ url, target, features: features ?? null }); return {}; };

  const list = w.document.getElementById('list');
  w.bindNavClicks(list, (e) => {
    if (e.target.closest('button')) return null;
    const row = e.target.closest('.row');
    return row ? row.dataset.href : null;
  });

  const fire = (id, type, init) => {
    navigations.length = 0; opened.length = 0;
    const el = w.document.getElementById(id);
    const ev = new w.MouseEvent(type, { bubbles: true, cancelable: true, ...init });
    const notCancelled = el.dispatchEvent(ev);
    return {
      navigated: navigations.length, opened: opened.slice(),
      defaultPrevented: !notCancelled,
    };
  };

  return {
    errors,
    helper_defined: typeof w.bindNavClicks === 'function' && typeof w.followHref === 'function',
    plain: fire('cell', 'click', { button: 0 }),
    ctrl: fire('cell', 'click', { button: 0, ctrlKey: true }),
    meta: fire('cell', 'click', { button: 0, metaKey: true }),
    shift: fire('cell', 'click', { button: 0, shiftKey: true }),
    middle: fire('cell', 'auxclick', { button: 1 }),
    right_aux: fire('cell', 'auxclick', { button: 2 }),
    middle_as_click: fire('cell', 'click', { button: 1 }),
    ctrl_on_action: fire('act', 'click', { button: 0, ctrlKey: true }),
    middle_on_action: fire('act', 'auxclick', { button: 1 }),
    plain_on_action: fire('act', 'click', { button: 0 }),
  };
}

process.stdout.write(JSON.stringify(run(), null, 2));
