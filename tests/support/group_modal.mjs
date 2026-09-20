/* Drives the shared "Group Channels" modal (static/js/group-modal.js) in jsdom and prints
   what it observed as JSON. Called by tests/test_group_modal_js.py, which owns every
   assertion - this file only reports, so a failure reads as "the modal did X" in Python
   rather than as a node exit code. Same shape as tests/support/filter_bar.mjs.

   Why a real DOM: both things under test are states the modal passes THROUGH, not strings
   it returns. "Add Channels is disabled while the suggest fetch is out and live again once
   it settles" cannot be observed from the source, and neither can "the picked channels are
   list rows, not badges" - that one is built by a helper this file evaluates the shipped
   util.js to reach (escHtml, buildModal). jsonFetch is the one thing stubbed, because the
   whole point is to hold a request open and look at the modal while it is out.

   jsdom computes no layout, so nothing about how the loading row or the picked list LOOK -
   where the spinner sits, whether the list scrolls at nine rows, 375px - can be asked
   here. Those need a browser.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const JS = ['util.js', 'group-modal.js'].map(f => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));

const PAGE = '<!doctype html><html><head><meta name="csrf-token" content="t"></head>'
  + '<body></body></html>';

const SUGGEST = [
  { channel_id: 11, channel_name: 'Fox Sports 1 HD', account_name: 'Acct A',
    account_color: '#ff0000', reason: 'epg_id', format_status: 'confirmed',
    format: '1920x1080', resolution: '1920x1080', fps: 60, bitrate_kbps: 5000,
    health_score: 92, selectable: true },
  { channel_id: 12, channel_name: 'FS1 backup', account_name: 'Acct B',
    account_color: '#00ff00', reason: 'name', format_status: 'unverified',
    resolution: null, fps: null, bitrate_kbps: null, health_score: null, selectable: true },
];

/* A fresh window per scenario: openGroupModal writes into document.body and each scenario
   answers the same URL differently, so one shared window would have them stepping on each
   other's modal. */
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
  JS.forEach((src) => w.eval(src));

  /* The stub replaces util.js's real jsonFetch and hands back a promise this harness
     settles by hand - built inside the window so the modal's `.then` runs in the same
     realm the rest of it does. */
  w.eval(`
    var ASKED = [];
    var PENDING = {};
    jsonFetch = function (url) {
      ASKED.push(url);
      return new Promise(function (res, rej) { PENDING[url] = { res: res, rej: rej }; });
    };
    function q(sel) { return document.querySelector(sel); }
    function shown(sel) {
      var el = q(sel);
      return el ? el.style.display !== 'none' : null;
    }
    function btn(which) {
      var all = [].slice.call(document.querySelectorAll('#group-modal .modal-foot .btn'));
      return which === 'submit' ? q('#group-modal .modal-foot .btn-primary') : all[0];
    }
    function snap() {
      var sub = btn('submit');
      var cancel = btn('cancel');
      var empty = q('#group-suggest-empty');
      var err = q('#group-modal-error');
      return {
        loading: shown('#group-suggest-loading'),
        loading_text: (q('#group-suggest-loading') || {}).textContent
          ? q('#group-suggest-loading').textContent.trim() : '',
        suggest_shown: shown('#group-suggest-wrap'),
        suggest_rows: document.querySelectorAll('#group-modal .group-suggest-cb').length,
        submit_label: sub ? sub.textContent.trim() : null,
        submit_disabled: sub ? sub.disabled : null,
        cancel_disabled: cancel ? cancel.disabled : null,
        empty_shown: shown('#group-suggest-empty'),
        empty_text: empty ? empty.textContent.trim() : '',
        error_shown: shown('#group-modal-error'),
        error_text: err ? err.textContent.trim() : '',
        picked_rows: [].slice.call(document.querySelectorAll('#group-modal .cg-picked-row'))
          .map(function (r) { return r.textContent.trim(); }),
        picked_dots: document.querySelectorAll('#group-modal .cg-picked-row .color-dot').length,
        badges: document.querySelectorAll('#group-modal .badge').length,
        abort_badges: document.querySelectorAll('#group-modal .badge.b-abort').length,
        picked_label: q('#group-modal .gd-field-lbl')
          ? q('#group-modal .gd-field-lbl').textContent.trim() : null,
        suggest_badges: [].slice.call(document.querySelectorAll('#group-modal .suggest-table tbody tr'))
          .map(function (tr) {
            var b = tr.querySelector('.st-ch .badge');
            return { name: tr.querySelector('.channel-name-link').textContent.trim(),
                     badge: b ? b.textContent.trim() : null,
                     tip: b ? b.getAttribute('data-tip') : null,
                     checkable: !tr.querySelector('.group-suggest-cb').disabled };
          }),
        asked: ASKED.slice(),
      };
    }
  `);
  return { w, errors };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

const out = { errors: [] };

async function scenario(name, openExpr, steps) {
  const { w, errors } = boot();
  w.eval(openExpr);
  await tick();
  const frames = { open: w.eval('snap()') };
  for (const [label, expr] of steps) {
    w.eval(expr);
    await tick();
    frames[label] = w.eval('snap()');
  }
  out[name] = frames;
  out.errors.push(...errors);
}

const GROUP = "{ id: 7, name: 'Fox Sports 1' }";
const SUGGEST_URL = '/api/channel-groups/suggest?group_id=7';
const RESOLVE = (payload) =>
  `PENDING['${SUGGEST_URL}'].res(${JSON.stringify(payload)})`;
const REJECT = `PENDING['${SUGGEST_URL}'].rej(new Error('Service Unavailable'))`;

// 1. The group page's "+ Add Matching Channels": opens on an empty body while the
//    suggestions are out, then fills.
await scenario('fixed_results',
  `openGroupModal({ channels: [], fixedGroup: ${GROUP}, onDone: function () {} })`,
  [['settled', RESOLVE({ results: SUGGEST, seed_format_known: true, seed_format: '1080p60' })]]);

// 2. Same, but the search comes back empty - nothing to add, and it says so.
await scenario('fixed_empty',
  `openGroupModal({ channels: [], fixedGroup: ${GROUP}, onDone: function () {} })`,
  [['settled', RESOLVE({ results: [] })]]);

// 3. Same, but the request fails. The old code swallowed this entirely.
await scenario('fixed_error',
  `openGroupModal({ channels: [], fixedGroup: ${GROUP}, onDone: function () {} })`,
  [['settled', REJECT]]);

// 4. The channel search's "adding to a group" context: a fixed group WITH a selection, so
//    an empty suggest result still leaves something the button can send.
await scenario('fixed_with_selection',
  `openGroupModal({ channels: [{ channel_id: 3, channel_name: 'FS1 East',`
  + ` account_color: '#abcdef', account_name: 'Acct A' }],`
  + ` fixedGroup: ${GROUP}, onDone: function () {} })`,
  [['settled', RESOLVE({ results: [] })]]);

// 5. A multi-channel selection with no fixed group: the picked list, and no suggest fetch.
await scenario('selection_only',
  `openGroupModal({ channels: [`
  + `{ channel_id: 1, channel_name: 'Fox Sports 1', account_color: '#abcdef', account_name: 'Acct A' },`
  + `{ channel_id: 2, channel_name: '<b>FS1</b> HD' }`
  + `], onDone: function () {} })`,
  []);

// 6. The group detail page's per-row "Group with duplicates": one channel, so suggestions
//    ARE fetched - but this path never opened empty and is deliberately unchanged.
await scenario('single_seed',
  `openGroupModal({ channels: [{ channel_id: 5, channel_name: 'FS1' }], onDone: function () {} })`,
  [['settled', "PENDING['/api/channel-groups/suggest?channel_id=5'].res({ results: [] })"]]);

// 7. A suggestion the provider has already dropped: marked Missing, still listed and
//    still checkable (dev/changelog/1042).
await scenario('fixed_missing',
  `openGroupModal({ channels: [], fixedGroup: ${GROUP}, onDone: function () {} })`,
  [['settled', RESOLVE({ seed_format_known: true, seed_format: '1080p60', results: [
    Object.assign({}, SUGGEST[0], { lifecycle: null, lifecycle_date: '' }),
    Object.assign({}, SUGGEST[1], { lifecycle: 'missing', lifecycle_date: '2026-09-01' }),
  ] })]]);

console.log(JSON.stringify(out));
