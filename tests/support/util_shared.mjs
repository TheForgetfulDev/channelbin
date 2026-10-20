/* Drives util.js's shared page pieces in jsdom and prints what it observed as JSON:
   MOBILE_MQ / isPhone() (the one breakpoint), readBootJson() (the one boot-block reader)
   and openMenuSheet() (the one phone bottom sheet). Called by tests/test_util_shared_js.py,
   which owns every assertion - this file only reports. Same shape as
   tests/support/confirm_modal.mjs: the shipped util.js is evaluated as base.html's
   <script src> would have, not stubbed.

   window.matchMedia is stubbed BEFORE util.js runs, with the width argv names, because
   jsdom's own answers `matches: false` for everything and the breakpoint has to be
   drivable. argv: <repo root> <viewport width px>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, WIDTH] = process.argv;
const UTIL = fs.readFileSync(`${REPO}/static/js/util.js`, 'utf8');

const PAGE = `<!doctype html><html><head><meta name="csrf-token" content="t"></head><body>
  <script type="application/json" id="good-boot">{"answer": 42, "list": [1, 2]}</script>
  <script type="application/json" id="bad-boot">{not json</script>
  <div class="arow" data-name="Row name">
    <button data-menu aria-label="Actions">&#8943;</button>
    <div class="menu">
      <button class="menu-item" data-act="sync" data-account-id="7">Sync now</button>
      <a class="menu-item" href="/accounts/7/channels">Browse channels &rarr;</a>
      <button class="menu-item" data-act="pause" disabled>Pause</button>
      <div class="sep"></div>
      <button class="menu-item danger" data-act="delete" data-account-id="7">Delete account</button>
    </div>
  </div>
</body></html>`;

const errors = [];
const warnings = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
vc.on('warn', (...a) => warnings.push(a.map(String).join(' ')));
const listeners = [];
const dom = new JSDOM(PAGE, {
  runScripts: 'dangerously', pretendToBeVisual: true,
  url: 'http://localhost:5000/', virtualConsole: vc,
  beforeParse(w) {
    w.matchMedia = (query) => {
      const max = /max-width:\s*(\d+)px/.exec(query);
      return {
        media: query, matches: max ? Number(WIDTH) <= Number(max[1]) : true, onchange: null,
        addEventListener(type, fn) { listeners.push({ query, type, fn }); },
        removeEventListener() {}, addListener() {}, removeListener() {},
        dispatchEvent() { return false; },
      };
    };
  },
});
const w = dom.window;
const d = w.document;
w.eval(UTIL);
const MOBILE_MQ = w.eval('MOBILE_MQ');
const isPhone = w.eval('isPhone');
const readBootJson = w.eval('readBootJson');
const openMenuSheet = w.eval('openMenuSheet');

const out = { errors, warnings, width: Number(WIDTH) };

// ── The breakpoint ───────────────────────────────────────────────────────────
out.mq = { media: MOBILE_MQ.media, isPhone: isPhone() };
// A page listening for the breakpoint listens on the one object util.js built.
MOBILE_MQ.addEventListener('change', () => {});
out.mq.listenerQuery = listeners.length ? listeners[0].query : null;

// ── The boot-block reader ────────────────────────────────────────────────────
out.boot = {
  good: readBootJson('good-boot'),
  missingNull: readBootJson('missing-boot'),
  missingFallback: readBootJson('missing-boot', {}),
  warningsBeforeBad: warnings.length,
  bad: readBootJson('bad-boot', { fell: 'back' }),
  warningsAfterBad: warnings.length,
};

// ── The bottom sheet, from a menu ────────────────────────────────────────────
const overlays = () => d.querySelectorAll('.modal').length;
const menu = d.querySelector('.menu');
const acts = [];
let bubbled = 0;
d.addEventListener('click', (e) => { if (e.target.closest('.acct-sheet')) bubbled += 1; });
openMenuSheet(menu, {
  title: 'Row name',
  onAct: (act, copy, original) => acts.push({
    act, copyIsInSheet: !!copy.closest('.acct-sheet'), overlaysWhenCalled: overlays(),
    originalIsMenuChild: !!(original && original.parentElement === menu),
    originalAct: original ? original.dataset.act : null,
  }),
});
const sheet = d.querySelector('.acct-sheet');
out.menuSheet = {
  title: d.querySelector('.modal-head h2').textContent,
  rows: Array.from(sheet.children).map((el) => ({
    tag: el.tagName, cls: el.className, text: el.textContent.trim(),
    act: el.dataset ? el.dataset.act || null : null, disabled: !!el.disabled,
    href: el.getAttribute ? el.getAttribute('href') : null,
    accountId: el.dataset ? el.dataset.accountId || null : null,
  })),
  menuStillHasItems: menu.children.length,
  overlaysBefore: overlays(),
};
// A disabled row does nothing; the danger row closes the sheet first and then acts.
sheet.querySelector('[data-act="pause"]').click();
out.menuSheet.actsAfterDisabledTap = acts.length;
sheet.querySelector('[data-act="delete"]').click();
out.menuSheet.acts = acts;
out.menuSheet.overlaysAfter = overlays();
out.menuSheet.bubbledToDocument = bubbled;

// ── The bottom sheet, from an item list ──────────────────────────────────────
const listActs = [];
openMenuSheet([
  { act: 'block', label: 'Block account use' },
  { href: '/accounts/7/channels', label: 'Browse channels' },
  { sep: true },
  { act: 'force-epg', label: 'Force EPG resync', disabled: true },
  { act: 'delete', label: 'Delete account', danger: true },
], { title: 'Account', onAct: (act, copy, original) => listActs.push({ act, original }) });
const list = d.querySelector('.acct-sheet');
out.listSheet = {
  rows: Array.from(list.children).map((el) => ({
    tag: el.tagName, cls: el.className, text: el.textContent.trim(),
    act: el.dataset ? el.dataset.act || null : null, disabled: !!el.disabled,
    href: el.getAttribute ? el.getAttribute('href') : null,
  })),
};
list.querySelector('[data-act="block"]').click();
out.listSheet.acts = listActs;
out.listSheet.overlaysAfter = overlays();

process.stdout.write(JSON.stringify(out));
