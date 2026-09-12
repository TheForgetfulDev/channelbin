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

   jsdom computes no layout, so where the tooltip lands, its flip near the viewport edge and
   the sticky-header clamp cannot be asked here - those need a browser.

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
</body></html>`;

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

  return {
    errors,
    macro: observe('macro'),
    markup: observe('markup'),
    plain: observe('plain'),
  };
}

process.stdout.write(JSON.stringify(boot(), null, 2));
