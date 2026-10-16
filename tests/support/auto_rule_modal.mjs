/* Drives the "Create rule from this search" dialog (static/js/auto-rule-modal.js) in jsdom
   and prints what it observed as JSON. Called by tests/test_auto_rule_modal_js.py, which
   owns every assertion. Same shape as tests/support/bulk_schedule_modal.mjs: util.js is
   evaluated for real, and jsonFetch and showToast are the two things stubbed.

   argv: <repo root> <fixture json>. The fixture carries the phrase table the routes really
   serve (routes/auto_rules.py::verdict_phrases()) and the count dicts to word with it. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO, FIXTURE] = process.argv;
const FIX = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const JS = ['util.js', 'auto-rule-modal.js']
  .map((f) => fs.readFileSync(`${REPO}/static/js/${f}`, 'utf8'));
const PAGE = '<!doctype html><html><head><meta name="csrf-token" content="t"></head>'
  + '<body></body></html>';

function boot(answer) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));
  const dom = new JSDOM(PAGE, { runScripts: 'dangerously', pretendToBeVisual: true,
                                url: 'http://localhost:5000/channels', virtualConsole: vc });
  const w = dom.window;
  JS.forEach((src) => w.eval(src));
  w.SENT = [];
  w.TOASTS = [];
  w.ANSWER = answer;
  w.eval(`
    jsonFetch = function (url, opts) {
      SENT.push({ url: url, method: opts.method, body: JSON.parse(opts.body) });
      if (ANSWER.reject) return Promise.reject(new Error(ANSWER.reject));
      return Promise.resolve(ANSWER);
    };
    showToast = function (msg, o) {
      o = o || {};
      TOASTS.push({ msg: msg, type: o.type || 'success', html: !!o.html });
    };
  `);
  return { w, errors };
}

const settle = (w) => new Promise((r) => w.setTimeout(r, 0));
const $ = (w, sel) => w.document.querySelector(sel);
const $$ = (w, sel) => Array.from(w.document.querySelectorAll(sel));

function open(w, saved) {
  return w.openAutoRuleModal({
    search: 'q=nascar&grain=airings',
    summary: '"nascar"',
    name: 'nascar',
    profiles: [{ id: 5, name: 'Padded' }],
    defaultCap: 10,
    phrases: FIX.phrases,
    createUrl: '/api/auto-rules',
    rulesUrl: '/rules',
    onSaved: (res) => saved.push(res),
  });
}

function setValue(w, sel, value) {
  const el = $(w, sel);
  el.value = value;
  el.dispatchEvent(new w.Event('input', { bubbles: true }));
  el.dispatchEvent(new w.Event('change', { bubbles: true }));
}

function snapshot(w, saved, errors) {
  const err = $(w, '#ar-err');
  const btn = $(w, '.modal-foot .btn-primary');
  return {
    open: !!$(w, '.modal'),
    error_shown: !!err && !err.hidden,
    error: err ? err.textContent : null,
    button_label: btn ? btn.textContent : null,
    button_disabled: btn ? btn.disabled : null,
    sent: w.SENT,
    toasts: w.TOASTS,
    saved: saved.length,
    errors,
  };
}

/* Fill the form, press Create rule, report. `fill(w)` sets the fields for the scenario. */
async function scenario(answer, fill) {
  const { w, errors } = boot(answer);
  const saved = [];
  open(w, saved);
  const first = {
    title: $(w, '.modal-head h2').textContent,
    name: $(w, '#ar-name').value,
    profile_options: $$(w, '#ar-profile option').map((o) => [o.value, o.textContent]),
    repeats_checked: $(w, '#ar-repeats').checked,
    cap_placeholder: $(w, '#ar-cap').getAttribute('placeholder'),
    footer: $$(w, '.modal-foot .btn').map((b) => [b.textContent, b.className]),
    intro: $(w, '.modal-body .card-note').textContent.replace(/\s+/g, ' ').trim(),
    text: $(w, '.modal-body').textContent.replace(/\s+/g, ' ').trim(),
    error_hidden_at_open: $(w, '#ar-err').hidden,
  };
  if (fill) fill(w);
  $(w, '.modal-foot .btn-primary').click();
  await settle(w);
  await settle(w);
  return Object.assign({ first }, snapshot(w, saved, errors));
}

const EV_OK = { queued: false, rule_id: 1, scheduled: 2, skipped: { skipped_repeat: 1 },
                error: null, abandoned: false, guide_reach: '2026-10-16T23:00:00' };

const out = {
  clean: await scenario({ success: true, rule: { id: 1 }, evaluation: EV_OK }, (w) => {
    setValue(w, '#ar-name', '  NASCAR Cup  ');
    setValue(w, '#ar-profile', '5');
    setValue(w, '#ar-limit-count', '1');
    setValue(w, '#ar-limit-hours', '120');
  }),
  defaults: await scenario({ success: true, rule: { id: 1 }, evaluation: EV_OK }, null),
  // The routes answer a create with the new rule's page (dev/changelog/1189).
  with_page: await scenario({ success: true, rule: { id: 4 }, url: '/rules/4', evaluation: EV_OK },
    (w) => setValue(w, '#ar-name', 'Cup <Series>')),
  refused: await scenario(
    { reject: 'A rule cannot use "On now" - it never joins a program that has already started.' },
    (w) => { setValue(w, '#ar-cap', '3'); $(w, '#ar-repeats').checked = false; }),
  half_limit: await scenario({ success: true, evaluation: EV_OK },
    (w) => setValue(w, '#ar-limit-hours', '120')),
  no_name: await scenario({ success: true, evaluation: EV_OK }, (w) => setValue(w, '#ar-name', '   ')),
  queued: await scenario(
    { success: true, evaluation: { queued: true, waiting_for: 'an account sync is already running (Acct)' } },
    null),
  empty_guide: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 0, skipped: {},
                                   error: null, abandoned: false, guide_reach: null } },
    null),
  nothing_matched: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 0, skipped: {},
                                   error: null, abandoned: false, guide_reach: '2026-10-16T23:00:00' } },
    null),
  placed_with_problems: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 2, skipped: {},
                                   already_scheduled: 0, error: null,
                                   problems: '"Cup Race" on Sun Oct 18, 3:00 PM EDT could not be placed: the guide changed while it was being checked.',
                                   abandoned: false, guide_reach: '2026-10-16T23:00:00' } },
    null),
  all_already_scheduled: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 0, skipped: {},
                                   already_scheduled: 3, error: null, problems: null,
                                   abandoned: false, guide_reach: '2026-10-16T23:00:00' } },
    null),
  moved_with_its_program: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 0, skipped: {},
                                   already_scheduled: 1, retracked: 1, error: null,
                                   problems: null, abandoned: false,
                                   guide_reach: '2026-10-16T23:00:00' } },
    null),
  check_error: await scenario(
    { success: true, evaluation: { queued: false, rule_id: 1, scheduled: 0, skipped: {},
                                   error: 'The search names a tag that is gone: "live".',
                                   abandoned: false, guide_reach: null } },
    null),
};

/* The question a changed search asks (openAutoRuleUnmatched, dev/changelog/1203). `answers`
   are what save() does with each press, in order: an object resolves, {reject, status, data}
   throws the way jsonFetch does. `pick` chooses a radio before the press. */
const TWO = [{ id: 41, name: 'Cup Race - Alpha', when: 'Sun Oct 18, 3:00 PM' },
             { id: 42, name: 'Cup Race - Bravo', when: 'Sun Oct 25, 3:00 PM' }];
async function unmatchedScenario(data, answers, pick, presses = 1) {
  const { w, errors } = boot({});
  const saves = [];
  const done = [];
  w.openAutoRuleUnmatched({
    ruleName: 'Cup <Series>', data,
    save: (answer) => {
      saves.push(answer);
      const a = answers.shift();
      if (a && a.reject) {
        const e = new w.Error(a.reject);
        e.status = a.status;
        e.data = a.data;
        return Promise.reject(e);
      }
      return Promise.resolve(a);
    },
    onSaved: (res) => done.push(res),
  });
  const first = {
    title: $(w, '.modal-head h2').textContent,
    text: $(w, '.modal-body').textContent.replace(/\s+/g, ' ').trim(),
    listed: $$(w, '.statlist .srow').map((r) => [r.querySelector('.sk').textContent,
                                                r.querySelector('.sv').textContent]),
    checked: ($(w, 'input[name="ru-fate"]:checked') || {}).value || null,
    choices: $$(w, '.fp-radio strong').map((e) => e.textContent),
    footer: $$(w, '.modal-foot .btn').map((b) => [b.textContent, b.className]),
  };
  for (let i = 0; i < presses; i += 1) {
    if (pick) $(w, `input[name="ru-fate"][value="${pick}"]`).checked = true;
    $(w, '.modal-foot .btn-primary').click();
    await settle(w);
    await settle(w);
  }
  const err = $(w, '#ru-err');
  return {
    first, saves, done: done.length, open: !!$(w, '.modal'),
    error_shown: !!err && !err.hidden, error: err ? err.textContent : null,
    listed_after: $$(w, '.statlist .sk').map((e) => e.textContent),
    checked_after: ($(w, 'input[name="ru-fate"]:checked') || {}).value || null,
    errors,
  };
}

out.unmatched_cancel = await unmatchedScenario(
  { unmatched: TWO, determined: true, reason: null }, [{ success: true, cancelled: 2 }], null);
out.unmatched_keep = await unmatchedScenario(
  { unmatched: TWO.slice(0, 1), determined: true, reason: null },
  [{ success: true, kept: 1 }], 'keep');
out.unmatched_undetermined = await unmatchedScenario(
  { unmatched: TWO, determined: false,
    reason: 'The new search matches more than 1,000 upcoming showings, which is more than a rule will run.' },
  [], null, 0);
out.unmatched_grew = await unmatchedScenario(
  { unmatched: TWO.slice(0, 1), determined: true, reason: null },
  [{ reject: 'asked again', status: 409,
     data: { unmatched: TWO, determined: true, reason: null } }], 'keep');
out.unmatched_refused = await unmatchedScenario(
  { unmatched: TWO, determined: true, reason: null },
  [{ reject: 'Rule not found.', status: 404, data: { error: 'Rule not found.' } }], null);

// The pure helpers, over the real phrase table.
const { w } = boot({});
out.counts_text = FIX.counts.map((c) => w.autoRuleCountsText(c, FIX.phrases));
out.numbers = ['', '  ', ' 7 ', '0', '1.5', '-2', null].map((v) => w.autoRuleNumber(v));
out.unmatched_text = [{}, { cancelled: 1 }, { cancelled: 2, kept: 0 }, { kept: 1 },
                      { cancelled: 1, kept: 3 }].map((r) => w.autoRuleUnmatchedText(r));
process.stdout.write(JSON.stringify(out));
