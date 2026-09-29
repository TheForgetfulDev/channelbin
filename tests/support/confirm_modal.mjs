/* Drives util.js::confirmModal() in jsdom and prints what it observed as JSON. Called by
   tests/test_confirm_modal_js.py, which owns every assertion - this file only reports.
   Same shape as tests/support/filter_bar.mjs: the shipped util.js is evaluated the way
   base.html's <script src> would have been, not stubbed.

   The page carries a hand-rolled overlay already open and a document-level Escape
   listener, standing in for the guide's record modal and its own Escape handler - the
   case a confirm has to survive, since it usually opens over another overlay.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <div id="host-modal" class="modal" style="display:flex"><div class="modal-panel">host</div></div>
</body></html>`;

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/', virtualConsole: vc,
});
const w = dom.window;
const d = w.document;
w.eval(UTIL);

// The host page's own Escape handler, registered first exactly as guide.js's is.
let hostEscapes = 0;
d.addEventListener('keydown', (e) => { if (e.key === 'Escape') hostEscapes += 1; });

const overlays = () => d.querySelectorAll('.modal:not(#host-modal)').length;
const escape = () => d.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

async function run(spec, act) {
  const p = w.confirmModal(spec);
  const ov = d.querySelector('.modal:not(#host-modal)');
  const btns = Array.from(ov.querySelectorAll('.modal-foot .btn'));
  const shown = {
    title: ov.querySelector('.modal-head h2').textContent,
    paras: Array.from(ov.querySelectorAll('.modal-body p')).map((el) => el.textContent),
    paraHtml: Array.from(ov.querySelectorAll('.modal-body p')).map((el) => el.innerHTML),
    list: Array.from(ov.querySelectorAll('.modal-body li')).map((el) => el.textContent),
    buttons: btns.map((b) => b.textContent),
    confirmClass: btns[btns.length - 1].className,
    focused: d.activeElement ? d.activeElement.textContent : null,
  };
  const before = hostEscapes;
  act(ov, btns);
  const result = await p;
  return {
    ...shown, result,
    overlaysAfter: overlays(),
    hostStillOpen: !!d.getElementById('host-modal'),
    hostSawEscape: hostEscapes - before,
  };
}

const plain = { title: 'Stop recording', message: 'It stops.', confirmLabel: 'Stop' };
const out = { errors };
out.confirm = await run(plain, (ov, b) => b[1].click());
out.cancel = await run(plain, (ov, b) => b[0].click());
out.close = await run(plain, (ov) => ov.querySelector('.modal-close').click());
out.backdrop = await run(plain, (ov) => ov.querySelector('.modal-backdrop').click());
out.escape = await run(plain, () => escape());
out.rich = await run({
  title: 'Abort recording', message: 'Capture of <b>X</b> stops.',
  consequence: 'Segments are deleted.', list: ['<i>A</i>', 'B'],
  confirmLabel: 'Abort', danger: true,
}, (ov, b) => b[0].click());

// After every confirm has closed, Escape belongs to the host again - no listener leaked.
const before = hostEscapes;
escape();
out.escapeAfterAllClosed = hostEscapes - before;

console.log(JSON.stringify(out));
