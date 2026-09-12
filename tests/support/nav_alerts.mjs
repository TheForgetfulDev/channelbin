/* Drives static/js/nav-alerts.js in jsdom against a page the real app rendered, and
   prints what it observed as JSON. Called by tests/test_nav_alerts_js.py, which owns every
   assertion - this file only reports. Same shape as tests/support/filter_bar.mjs.

   The page's own <script> blocks are stripped and the shipped files evaluated in their
   place: util.js (buildModal, jsonFetch, menus, confirmIgnoreAlert), nav-alerts.js, and
   base.html's collapsed-rail block, lifted out of the rendered HTML, because the rail tip
   is derived from the counts this script writes. fetch is stubbed before util.js wraps it.

   jsdom computes no layout, so the ellipsis on a long title and the 375px wrap are browser
   work.

   argv: <repo root> <rendered page html path>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, PAGE_PATH] = process.argv;
const rendered = fs.readFileSync(PAGE_PATH, 'utf8');
const js = (f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8');

function railSource() {
  const anchor = rendered.indexOf('const applyRailTips');
  if (anchor < 0) throw new Error('no applyRailTips in the rendered page');
  const start = rendered.lastIndexOf('(function', anchor);
  const end = rendered.indexOf('})();', anchor);
  if (start < 0 || end < 0) throw new Error('could not bound the rail IIFE');
  return rendered.slice(start, end + 5);
}

const PAYLOAD = {
  count: 6, error_count: 2, warn_count: 1, more: 2,
  banner: {
    id: 9, severity: 'ERROR', title: 'Move failed: /dvr-complete is not reachable',
    body: 'Line one\nLine two', created_at: '2026-09-11T05:07:00',
    created_label: 'Sep 11, 2026 01:07 AM EDT', created_short: '9/11/26 1:07 AM',
    created_age: '9d 2h ago', link: '/accounts/2', link_label: 'Account',
    is_active_problem: false,
  },
};
const WARN_ONLY = {
  count: 3, error_count: 0, warn_count: 1, more: 0,
  banner: { id: 4, severity: 'WARN', title: 'EPG fetch failed', body: '', created_label: 'x',
    link: null, link_label: null, is_active_problem: false },
};
// A problem that is still true and clears itself: its details view carries the "Still
// active" notice, in the severity's own color (dev/changelog/932).
const ACTIVE_ERROR = {
  count: 2, error_count: 1, warn_count: 0, more: 0,
  banner: { id: 12, severity: 'ERROR', title: 'DVR output directory is unusable',
    body: 'It answers with Stale file handle.', created_label: 'x',
    link: null, link_label: null, is_active_problem: true },
};
const ACTIVE_WARN = {
  ...ACTIVE_ERROR,
  banner: { ...ACTIVE_ERROR.banner, id: 13, severity: 'WARN', title: 'EPG fetch failed' },
};

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 5; i++) await tick(); };

async function main() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const html = rendered.replace(/<script\b[\s\S]*?<\/script>/g, '');
  const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true,
    url: 'http://localhost:5000/', virtualConsole: vc });
  const w = dom.window;
  const d = w.document;

  const calls = [];
  let fetchOk = true;
  w.fetch = (url, opts = {}) => {
    calls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase() });
    const ok = fetchOk;
    return Promise.resolve({ ok, status: ok ? 200 : 500, statusText: ok ? 'OK' : 'Server Error',
      json: async () => (ok ? { success: true } : { error: 'boom' }) });
  };
  let refreshes = 0;
  w.fetchNavStatus = () => { refreshes++; };

  w.eval(js('util.js'));
  w.eval(js('nav-alerts.js'));
  w.eval(railSource());

  const banner = () => d.getElementById('alert-banner');
  const shownOf = (el) => el.style.display !== 'none';
  const apply = (p) => { w.__applyAlerts(JSON.parse(JSON.stringify(p))); w.__applyRailTips(); };
  const alertsTip = () => d.querySelector('.sidebar .nav-link[href="/alerts"]').getAttribute('data-tip');
  const nav = () => ({
    bad: [...d.querySelectorAll('.nav-count-bad')].map((e) => ({ text: e.textContent, shown: shownOf(e) })),
    warn: [...d.querySelectorAll('.nav-count-warn')].map((e) => ({ text: e.textContent, shown: shownOf(e) })),
    pips: [...d.querySelectorAll('.nav-pip.pip-alert')].map((e) => ({ shown: shownOf(e), bad: e.classList.contains('pip-bad') })),
  });
  const bannerState = () => {
    const b = banner();
    const title = d.getElementById('alert-banner-title');
    const more = d.getElementById('alert-banner-more');
    const time = d.getElementById('alert-banner-time');
    return {
      shown: shownOf(b), cls: b.className,
      sev: d.getElementById('alert-banner-sev').textContent,
      sevCls: d.getElementById('alert-banner-sev').className,
      title: title.textContent, titleInFirstSpan: title.parentElement === b.firstElementChild,
      // The stamp must stay OUT of the truncating first span: ellipsis clips paint, not
      // layout, so a sibling inside it is laid out past the clip and never seen.
      time: time.textContent, timeTip: time.getAttribute('data-tip'),
      timeOutsideFirstSpan: time.parentElement === b && time !== b.firstElementChild,
      more: more.textContent, moreShown: shownOf(more), moreTip: more.getAttribute('data-tip'),
    };
  };
  const modals = () => [...d.querySelectorAll('.modal')];
  const modalState = (m) => {
    const foot = m.querySelector('.modal-foot');
    const noticeEl = m.querySelector('.notice');
    const bodyEl = m.querySelector('.alert-detail-body');
    return {
      notice: noticeEl ? { cls: noticeEl.className, text: noticeEl.textContent } : null,
      // The notice has to sit ABOVE the body: below it, on a long traceback, it is off
      // screen and the alert reads as finished. DOCUMENT_POSITION_FOLLOWING === 4.
      noticeBeforeBody: !!(noticeEl && bodyEl
        && (noticeEl.compareDocumentPosition(bodyEl) & 4)),
      title: m.querySelector('.modal-head h2').textContent,
      buttons: [...foot.children].filter((c) => c.tagName === 'BUTTON')
        .map((b) => ({ label: b.textContent, cls: b.className })),
      firstChildIsMore: foot.firstElementChild.classList.contains('alert-detail-more'),
      menuItems: [...foot.querySelectorAll('.menu .menu-item')].map((b) => b.textContent),
      meta: m.querySelector('.alert-detail-meta') ? m.querySelector('.alert-detail-meta').textContent : null,
      body: m.querySelector('.alert-detail-body') ? m.querySelector('.alert-detail-body').textContent : null,
    };
  };
  const clickEl = (el) => {
    const ev = new w.MouseEvent('click', { bubbles: true, cancelable: true });
    el.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  const buttonNamed = (m, label) => [...m.querySelectorAll('.modal-foot button')].find((b) => b.textContent === label);
  const closeAll = () => { modals().forEach((m) => m.remove()); };
  const out = {};

  // Counts, pip and rail tip.
  d.body.classList.add('nav-min');
  apply(PAYLOAD);
  out.full = { nav: nav(), banner: bannerState(), tip: alertsTip() };
  apply(WARN_ONLY);
  out.warnOnly = { nav: nav(), banner: bannerState(), tip: alertsTip() };
  apply({ ...PAYLOAD, error_count: 1, warn_count: 0, more: 0 });
  out.oneError = { tip: alertsTip() };
  apply({ count: 4, error_count: 0, warn_count: 0, more: 0, banner: null });
  out.none = { nav: nav(), banner: { shown: shownOf(banner()) }, tip: alertsTip() };
  d.body.classList.remove('nav-min');

  // A counts-only call (the Alerts page's refresh) moves the counts and leaves the banner.
  apply(PAYLOAD);
  w.__applyAlerts({ count: 1, error_count: 0, warn_count: 1 });
  out.countsOnly = { nav: nav(), bannerShown: shownOf(banner()), bannerTitle: d.getElementById('alert-banner-title').textContent };

  // Details, with a destination.
  apply(PAYLOAD);
  const prevented = clickEl(d.getElementById('alert-banner-title'));
  out.details = { prevented, count: modals().length, modal: modals().length ? modalState(modals()[0]) : null };
  calls.length = 0; refreshes = 0;
  clickEl(buttonNamed(modals()[0], 'Mark read'));
  const hiddenAtOnce = !shownOf(banner());
  await settle();
  out.detailsMarkRead = { modalsLeft: modals().length, hiddenAtOnce, calls: [...calls], refreshes };
  closeAll();

  // Details, with no destination.
  apply(WARN_ONLY);
  clickEl(d.getElementById('alert-banner-title'));
  out.detailsNoLink = modals().length ? modalState(modals()[0]) : null;
  closeAll();

  // Details for a problem that is still happening, at both severity colors.
  apply(ACTIVE_ERROR);
  clickEl(d.getElementById('alert-banner-title'));
  out.detailsActiveError = modals().length ? modalState(modals()[0]) : null;
  closeAll();
  apply(ACTIVE_WARN);
  clickEl(d.getElementById('alert-banner-title'));
  out.detailsActiveWarn = modals().length ? modalState(modals()[0]) : null;
  closeAll();

  // Space on the title opens details too.
  apply(PAYLOAD);
  d.getElementById('alert-banner-title').dispatchEvent(new w.KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }));
  out.space = { count: modals().length };
  closeAll();

  // One-click x.
  apply(PAYLOAD);
  calls.length = 0; refreshes = 0;
  clickEl(d.getElementById('alert-banner-read'));
  const xHiddenAtOnce = !shownOf(banner());
  await settle();
  out.x = { hiddenAtOnce: xHiddenAtOnce, calls: [...calls], refreshes, modals: modals().length };

  // A failed mark-read says so and refreshes (which is what brings the alert back).
  apply(PAYLOAD);
  fetchOk = false; calls.length = 0; refreshes = 0;
  clickEl(d.getElementById('alert-banner-read'));
  await settle();
  out.xFailed = { toast: d.body.textContent.includes('Could not mark that alert read: boom'), refreshes };
  fetchOk = true;

  // Ignore, one level down behind the footer's ⋯.
  apply(PAYLOAD);
  clickEl(d.getElementById('alert-banner-title'));
  const details = modals()[0];
  clickEl(details.querySelector('.alert-detail-more [data-menu]'));
  const menuOpen = details.querySelector('.alert-detail-more .menu').classList.contains('open');
  clickEl(details.querySelector('.alert-detail-more .menu-item'));
  const afterIgnoreClick = modals().map((m) => m.querySelector('.modal-head h2').textContent);
  calls.length = 0; refreshes = 0;
  // Found by title rather than position, so a details view left open is reported as an
  // extra title above rather than crashing the run on a click at nothing.
  const confirmModal = modals().find((m) => m.querySelector('.modal-head h2').textContent === 'Ignore future alerts like this');
  const confirmBtn = confirmModal && buttonNamed(confirmModal, 'Ignore future alerts');
  if (confirmBtn) clickEl(confirmBtn);
  await settle();
  out.ignore = { menuOpen, afterIgnoreClick, calls: [...calls], refreshes, bannerShown: shownOf(banner()) };
  closeAll();

  out.errors = errors;
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
