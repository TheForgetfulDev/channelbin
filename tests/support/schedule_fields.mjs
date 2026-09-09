/* Drives the REAL static/js/schedule-fields.js (mountScheduleFields) in jsdom against a
   hand-built fragment matching exactly what templates/_macros.html::recur_schedule_fields
   renders for prefix 'x' with a window_label set - the "Run at: maintenance window / a
   specific time" radio this feature adds. Lighter than the full-page-render pattern
   tests/support/channel_search_page.mjs and friends use (no Flask app, no route render):
   this widget has no server data dependency, so a hand-built fragment is the accurate
   fixture, not a shortcut around one.

   Called by tests/test_check_window_js.py, which owns every assertion - this file only
   reports, so a failure reads as "the widget did X" in Python. jsdom computes no layout,
   so nothing about how the radio actually looks is asked here - only which mode payload()/
   value() reports and which inputs prefill() lands on, after real DOM events.

   argv: <repo root>. */
import fs from 'fs';
import { JSDOM, VirtualConsole } from 'jsdom';

const [, , REPO] = process.argv;
const SCHEDULE_FIELDS_JS = fs.readFileSync(`${REPO}/static/js/schedule-fields.js`, 'utf8');

const PAGE = `<!doctype html><html><body>
<div id="host"></div>
<template id="tpl">
  <div class="form-group" style="margin:0 0 0.5rem;">
    <label><input type="checkbox" id="x-recurring-cb"> Recurring</label>
  </div>
  <div id="x-oneoff-row" style="display:flex;">
    <div class="form-group"><input type="datetime-local" id="x-dt" class="form-control"></div>
  </div>
  <div id="x-recurring-row" style="display:none; flex-direction:column; gap:0.5rem;">
    <div class="form-group">
      <label class="gd-field-lbl" style="display:block;">Run at</label>
      <label><input type="radio" name="x-run-at" id="x-run-window" value="window" checked> Maintenance window (2:00 AM-6:00 AM)</label>
      <label><input type="radio" name="x-run-at" id="x-run-time" value="time"> A specific time</label>
    </div>
    <div style="display:flex;">
      <div class="form-group">
        <select id="x-recur-day">
          <option value="0">Every day</option>
          <option value="1">Sunday</option>
          <option value="2">Monday</option>
          <option value="3">Tuesday</option>
          <option value="4">Wednesday</option>
          <option value="5">Thursday</option>
          <option value="6">Friday</option>
          <option value="7">Saturday</option>
        </select>
      </div>
      <div class="form-group" id="x-recur-time-field">
        <input type="time" id="x-recur-time" class="form-control">
      </div>
    </div>
  </div>
</template>
</body></html>`;

function boot() {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  vc.on('error', (...a) => errors.push(`console.error: ${a.join(' ')}`));

  const dom = new JSDOM(PAGE, { runScripts: 'dangerously', virtualConsole: vc });
  const { window } = dom;
  window.eval(SCHEDULE_FIELDS_JS);

  const host = window.document.getElementById('host');
  const sched = window.mountScheduleFields({ host, templateId: 'tpl', prefix: 'x', tz: 'ET' });

  const q = (id) => window.document.getElementById(id);
  const fireChange = (el) => el.dispatchEvent(new window.Event('change', { bubbles: true }));

  const results = {};

  // 1. Default state: the maintenance-window radio is checked (macro default), and an
  //    untouched mount therefore needs no time typed to produce a valid payload.
  results.defaultPayload = sched.payload();
  results.defaultTimeFieldHidden = q('x-recur-time-field').style.display === 'none';

  // 2. The window radio drives payload() into window mode, no recur_time, and hides the
  //    (now irrelevant) time input.
  q('x-run-window').checked = true;
  fireChange(q('x-run-window'));
  results.windowModePayload = sched.payload();
  results.windowModeValue = sched.value();
  results.timeFieldHiddenInWindowMode = q('x-recur-time-field').style.display === 'none';

  // 3. Switching to the exact-time radio restores the time field and the required-time
  //    behavior.
  q('x-run-time').checked = true;
  fireChange(q('x-run-time'));
  results.timeFieldVisibleAfterSwitchBack = q('x-recur-time-field').style.display !== 'none';
  results.exactTimeNoTimePayload = sched.payload();
  q('x-recur-time').value = '04:15';
  fireChange(q('x-recur-time'));
  results.exactTimePayload = sched.payload();

  // 4. prefill() with use_window:true selects the window radio and hides the time field,
  //    without requiring the caller to also touch recur_time.
  sched.prefill({ recur_day: 3, use_window: true });
  results.prefillWindow = {
    day: q('x-recur-day').value,
    windowChecked: q('x-run-window').checked,
    timeFieldHidden: q('x-recur-time-field').style.display === 'none',
    payload: sched.payload(),
  };

  // 5. prefill() with an explicit use_window:false restores exact-time mode - this is
  //    what keeps a cloned or edited schedule winning over the rendered default.
  sched.prefill({ recur_day: 2, recur_time: '05:30', use_window: false });
  results.prefillExactTime = {
    day: q('x-recur-day').value,
    timeChecked: q('x-run-time').checked,
    timeFieldVisible: q('x-recur-time-field').style.display !== 'none',
    payload: sched.payload(),
  };

  // 6. prefill() with use_window ABSENT states no opinion, so whatever the radio already
  //    shows survives - the macro's rendered default is not overridden on mount. Run from
  //    the exact-time state case 5 left behind, so a pass cannot be the default's doing.
  sched.prefill({ recur_day: 5, recur_time: '06:45' });
  results.prefillNoOpinionFromTime = {
    day: q('x-recur-day').value,
    windowChecked: q('x-run-window').checked,
    timeChecked: q('x-run-time').checked,
    payload: sched.payload(),
  };

  // ...and the same from window mode, so it is "leave it alone" rather than "prefer time".
  q('x-run-window').checked = true;
  fireChange(q('x-run-window'));
  sched.prefill({ recur_day: 4 });
  results.prefillNoOpinionFromWindow = {
    day: q('x-recur-day').value,
    windowChecked: q('x-run-window').checked,
    timeFieldHidden: q('x-recur-time-field').style.display === 'none',
    payload: sched.payload(),
  };

  results.errors = errors;
  return results;
}

console.log(JSON.stringify(boot()));
