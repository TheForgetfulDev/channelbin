/* Drives util.js's one tooltip renderer in jsdom and prints what it observed as JSON.
   Called by tests/test_tooltip_entity_js.py, which owns every assertion - this file only
   reports, so a failure reads as a sentence about the tooltip rather than a node exit code.
   Same shape as tests/support/filter_bar.mjs.

   The two triggers below are the two ways a tip reaches the DOM, and they are NOT the same
   string by the time JS reads them back:
     - written into markup (a template's own data-tip, or a page script's innerHTML), where
       the HTML parser turns "&#10;" into a real newline before anything reads it;
     - passed as a Jinja macro argument, where autoescape writes "&amp;#10;", so the
       attribute really holds the six characters "&#10;".
   Both must end up rendering a line break (dev/docs/BUGS.md 2026-09-11).

   The third scenario is a page rebuilding the DOM under an open tooltip (the dashboard
   timeline's 60s tick): the tooltip must follow its anchor - re-measure one that survived,
   take over the node now under the pointer when it carries the same tip, and close only
   when what is under the pointer is something else. jsdom has no elementFromPoint and no
   layout, so the harness stubs both: elementFromPoint answers with the node the scenario
   names, and each anchor's getBoundingClientRect returns a rect of the scenario's choosing,
   so "re-placed onto the new anchor" is visible as the popover's `left` moving to that rect.

   Where the tooltip lands for real, its flip near the viewport edge and the sticky-header
   clamp still cannot be asked here - those need a browser.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

// "&amp;#10;" is what the server actually sends for a macro-argument tip; "&#10;" is what it
// sends for one written straight into markup. Both are spelled here exactly as they arrive.
const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <span id="macro" class="tip-plain" data-tip="Malformed URLs skipped (last sync).&amp;#10;Entries in the provider feed whose stream URL was not a usable URL.">250</span>
  <span id="markup" class="tip-plain" data-tip="How long the sync took.&#10;Bars are scaled against the longest run shown here.">Duration</span>
  <span id="plain" class="tip-plain" data-tip="One line, no break.">x</span>
  <div id="host"><span id="bar" class="tip-plain" data-tip="Live: Channel 5">bar</span></div>
  <span id="elsewhere" class="tip-plain" data-tip="Something else">y</span>
</body></html>`;

async function boot() {
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
  w.eval(UTIL);

  const observe = (id) => {
    const el = w.document.getElementById(id);
    // The shipped listener is delegated on document/mouseover, so the tooltip is opened the
    // way a real pointer opens it rather than by calling an internal.
    el.dispatchEvent(new w.MouseEvent('mouseover', { bubbles: true }));
    const pop = w.document.querySelector('.tip-pop');
    return {
      attribute: el.getAttribute('data-tip'),
      rendered: pop ? pop.textContent : null,
      displayed: pop ? pop.style.display : null,
    };
  };

  const result = {
    errors,
    macro: observe('macro'),
    markup: observe('markup'),
    plain: observe('plain'),
  };

  // MutationObserver callbacks are microtasks; one macrotask later they have all run.
  const settle = () => new Promise((r) => w.setTimeout(r, 0));
  const rectAt = (left) => () => ({ left, right: left + 40, top: 300, bottom: 320, width: 40, height: 20 });
  const pop = () => w.document.querySelector('.tip-pop');
  const hover = (el, x, y) => el.dispatchEvent(new w.MouseEvent('mouseover', { bubbles: true, clientX: x, clientY: y }));
  const host = w.document.getElementById('host');
  const rebuild = (tip) => {
    host.innerHTML = `<span id="bar" class="tip-plain" data-tip="${tip}">bar</span>`;
    const el = w.document.getElementById('bar');
    el.getBoundingClientRect = rectAt(300);
    return el;
  };
  const underPointer = (el) => { w.document.elementFromPoint = () => el; };

  // 1. The anchor survives and a sibling changes: the tooltip is re-measured in place.
  let bar = w.document.getElementById('bar');
  bar.getBoundingClientRect = rectAt(100);
  hover(bar, 110, 310);
  const leftAtHover = pop().style.left;
  bar.getBoundingClientRect = rectAt(200);          // the row slid, the node is the same
  w.document.getElementById('elsewhere').textContent = 'changed';
  await settle();
  const survivor = { leftAtHover, leftAfter: pop().style.left, displayed: pop().style.display };

  // 2. The anchor is replaced by a node carrying the same tip: the tooltip follows it.
  underPointer(rebuild('Live: Channel 5'));
  await settle();
  const sameTip = { left: pop().style.left, displayed: pop().style.display, text: pop().textContent };
  // ...and a mouseover on the replacement is a no-op, because it IS the anchor now.
  hover(w.document.getElementById('bar'), 310, 310);
  const reHover = { left: pop().style.left, displayed: pop().style.display };

  // 3. The anchor is replaced by a node carrying a different tip: the tooltip closes
  //    rather than describing something that is no longer under the pointer.
  underPointer(rebuild('Live: Channel 9'));
  await settle();
  const otherTip = { displayed: pop().style.display };

  // 4. Nothing with a tip is under the pointer after the rebuild: closed too.
  hover(w.document.getElementById('bar'), 310, 310);
  if (pop().style.display !== 'block') throw new Error('scenario 4 precondition: tooltip should be open');
  underPointer(w.document.getElementById('elsewhere').parentNode);
  rebuild('Live: Channel 9');
  await settle();
  const nothingUnder = { displayed: pop().style.display };

  result.rebuild = { survivor, sameTip, reHover, otherTip, nothingUnder };
  return result;
}

boot().then((r) => process.stdout.write(JSON.stringify(r, null, 2)));
