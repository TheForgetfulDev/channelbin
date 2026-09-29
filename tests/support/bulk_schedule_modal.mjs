/* Drives the "Schedule selected" modal (static/js/bulk-schedule-modal.js) in jsdom and
   prints what it observed as JSON. Called by tests/test_bulk_schedule_modal_js.py, which
   owns every assertion. Same shape as tests/support/group_modal.mjs: util.js is evaluated
   for real, and jsonFetch is the one thing stubbed, answering by URL.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const JS = ['util.js', 'bulk-schedule-modal.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const PAGE = '<!doctype html><html><head><meta name="csrf-token" content="t"></head>'
  + '<body></body></html>';

const PLANS = [
  { epg_id: 1, verdict: 'ok', name: 'Alpha', channel_name: 'One',
    start_time: '2026-10-01T18:00:00', stop_time: '2026-10-01T19:00:00', warnings: {} },
  { epg_id: 2, verdict: 'hard', name: 'Beta', channel_name: 'Two',
    start_time: '2026-10-01T18:00:00', stop_time: '2026-10-01T19:00:00',
    warnings: {
      connection_limit_warning: { message: 'This would exceed "Solo"\'s connection limit (1).',
        conflicts: [{ title: 'Alpha', channel_name: 'One', in_batch: true }] },
      overlap_warning: { message: 'Overlaps 1 other scheduled recording.', conflicts: [] },
    } },
  { epg_id: 3, verdict: 'warn', name: 'Gamma', group_name: 'Sports', member_name: 'Three',
    channel_name: 'Three', start_time: '2026-10-01T18:00:00', stop_time: '2026-10-01T19:00:00',
    warnings: { overlap_warning: { message: 'Overlaps 1 other scheduled recording.',
      conflicts: [{ title: 'Alpha', channel_name: 'One', in_batch: true }] } } },
  { epg_id: 4, verdict: 'skip', title: 'Delta', channel_name: 'Four',
    reason: 'Already scheduled.', warnings: {} },
];

function boot(scheduleAnswer) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://localhost:5000/', virtualConsole: vc });
  const w = dom.window;
  JS.forEach((src) => w.eval(src));
  w.SENT = [];
  w.TOASTS = [];
  w.PLANS = PLANS;
  w.SCHEDULE_ANSWER = scheduleAnswer;
  w.eval(`
    jsonFetch = function (url, opts) {
      SENT.push({ url: url, body: JSON.parse(opts.body) });
      if (url === '/prev') return Promise.resolve({ success: true, plans: PLANS });
      return Promise.resolve(SCHEDULE_ANSWER);
    };
    showToast = function (msg, o) { TOASTS.push({ msg: msg, type: (o || {}).type || 'ok' }); };
  `);
  return { w, errors };
}

const settle = (w) => new Promise((r) => w.setTimeout(r, 0));
const $ = (w, sel) => w.document.querySelector(sel);
const $$ = (w, sel) => Array.from(w.document.querySelectorAll(sel));

function open(w, done) {
  return w.openBulkScheduleModal({
    items: [1, 2, 3, 4].map((id) => ({ epg_id: id, group_id: id === 3 ? 9 : null })),
    profiles: [{ id: 5, name: 'Padded' }],
    previewUrl: '/prev',
    scheduleUrl: '/go',
    when: (p) => `${p.start_time}-${p.stop_time}`,
    onDone: (res) => done.push(res),
  });
}

async function scenario(answer) {
  const { w, errors } = boot(answer);
  const done = [];
  open(w, done);
  await settle(w);
  const submit = $(w, '.modal-foot .btn-primary');
  const obs = {
    title: $(w, '.modal-head h2').textContent,
    profile_options: $$(w, '#bs-profile option').map((o) => o.value),
    summary: $(w, '#bs-summary').textContent.trim(),
    rows: $$(w, '.bs-row').map((r) => ({
      title: r.querySelector('.bs-title').textContent,
      badge: (r.querySelector('.badge') || {}).textContent || '',
      skip: r.classList.contains('is-skip'),
      warn: r.querySelectorAll('.bs-warn').length,
      hard: !!r.querySelector('.bs-warn.is-hard'),
      text: r.textContent.replace(/\s+/g, ' ').trim(),
    })),
    submit_label: submit.textContent,
    submit_disabled: submit.disabled,
    first_preview_body: w.SENT[0] && w.SENT[0].body,
  };
  $(w, '#bs-profile').value = '5';
  $(w, '#bs-profile').dispatchEvent(new w.Event('change'));
  await settle(w);
  obs.profile_change_body = w.SENT[w.SENT.length - 1].body;
  submit.click();
  await settle(w);
  await settle(w);
  obs.schedule_request = w.SENT.find((s) => s.url === '/go') || null;
  obs.modal_open_after = !!$(w, '.modal');
  obs.failed_rows = $$(w, '.bs-row').map((r) => r.textContent.replace(/\s+/g, ' ').trim());
  obs.submit_after = !!$(w, '.modal-foot .btn-primary');
  obs.toasts = w.TOASTS;
  obs.on_done = done.length;
  obs.errors = errors;
  return obs;
}

const out = {
  clean: await scenario({ success: true, created: [{ epg_id: 1 }, { epg_id: 2 }, { epg_id: 3 }],
                          skipped: [{ epg_id: 4 }], failed: [] }),
  partial: await scenario({ success: true, created: [{ epg_id: 1 }, { epg_id: 3 }],
                            skipped: [{ epg_id: 4 }],
                            failed: [{ epg_id: 2, name: 'Beta', error: 'disk on fire' }] }),
};
process.stdout.write(JSON.stringify(out));
