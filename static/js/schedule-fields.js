/* Shared wiring for the health-check schedule picker: the `recur_schedule_fields`
   macro's inputs (templates/_macros.html), the recurring-vs-one-off row toggle, and
   the payload the on-demand-job endpoints expect.

   It exists because that wiring was already written twice - the old Browse tab's own
   template (`wireRecurToggle`) and static/js/group-detail.js's Settings modal - and
   check-modal.js would have been the third copy (CLAUDE.md "Search before you write").
   Both are now on this one: group-detail.js mounts it, and the Browse copy went with the
   template when the revamped channel search took over /channels.

   The macro's inputs carry ids, so the caller passes a `prefix` and every page keeps its
   own (`gdsched` for the detail page's Settings modal, `ccsched` for this one) - two
   copies of the same id on one page is the defect this indirection prevents.

   opts:
     host       - element to mount the fields into (its contents are replaced)
     templateId - id of the <template> holding the rendered macro
     prefix     - the id prefix that template was rendered with
     showRecurringCheckbox - keep the macro's own "Recurring" checkbox as the control
                  (the old Browse tab's shape). Default false: the caller drives the mode with
                  its own dropdown and the checkbox is hidden, never removed, so the
                  macro stays one thing.

   Returns { setMode, mode, prefill, value, payload, onChange }:
     setMode('manual'|'once'|'recur') - which row shows; 'manual' hides the whole block
     mode()      - the current mode
     prefill({oneoff_value, recur_day, recur_time, use_window}) - every key optional; an
                 omitted use_window leaves the macro's rendered "Run at" default alone
                 (see the macro's own note), an explicit true/false moves it
     value()     - {oneoff, recurDay, recurTime, useWindow} - live values, unvalidated
     payload()   - {recurring:true, recur_day, recur_time, use_window:false}
                   | {recurring:true, recur_day, use_window:true} (maintenance window - no
                     recur_time; app/check_window.py's dispatcher owns starting it)
                   | {recurring:false, scheduled_time}
                   | {error: '<message>'} when a required input is empty, so the caller
                   decides how to surface it. null in 'manual' mode. The window branch only
                   exists when the macro was rendered with a window_label - otherwise the
                   "Run at" radio doesn't exist and payload() behaves exactly as before.
     onChange(fn) - fn() on any input change, for a live readout
*/
function mountScheduleFields(opts) {
  const host = opts.host;
  const prefix = opts.prefix;
  const tpl = document.getElementById(opts.templateId);
  if (!tpl) throw new Error(`mountScheduleFields: no <template id="${opts.templateId}">`);
  host.innerHTML = '';
  host.appendChild(tpl.content.cloneNode(true));

  const q = (suffix) => host.querySelector(`#${prefix}-${suffix}`);
  const cb = q('recurring-cb');
  const oneoffRow = q('oneoff-row');
  const recurRow = q('recurring-row');
  const dt = q('dt');
  const day = q('recur-day');
  const time = q('recur-time');
  // Only present when the macro was rendered with a window_label (see _macros.html) -
  // absent for callers (e.g. the ad hoc "Test selected" one-off flow) that never offer
  // maintenance-window mode.
  const runWindow = q('run-window');
  const runTime = q('run-time');
  const timeField = q('recur-time-field');

  host.querySelectorAll('[data-tz-label]').forEach(el => { el.textContent = displayTz(); });
  if (!opts.showRecurringCheckbox) cb.closest('.form-group').style.display = 'none';

  let mode = 'recur';
  const listeners = [];
  const fire = () => listeners.forEach(fn => fn());

  function syncTimeFieldVisibility() {
    if (runWindow && timeField) timeField.style.display = runWindow.checked ? 'none' : '';
  }

  function setMode(next) {
    mode = next;
    cb.checked = next === 'recur';
    host.style.display = next === 'manual' ? 'none' : '';
    oneoffRow.style.display = next === 'once' ? 'flex' : 'none';
    recurRow.style.display = next === 'recur' ? 'flex' : 'none';
    fire();
  }

  cb.addEventListener('change', () => setMode(cb.checked ? 'recur' : 'once'));
  [dt, day, time].forEach(el => el.addEventListener('change', fire));
  [dt, time].forEach(el => el.addEventListener('input', fire));
  if (runWindow) {
    [runWindow, runTime].forEach(el => el.addEventListener('change', () => {
      syncTimeFieldVisibility();
      fire();
    }));
  }

  setMode(mode);
  syncTimeFieldVisibility();

  return {
    setMode,
    mode: () => mode,
    prefill(s) {
      if (!s) return;
      if (s.oneoff_value) dt.value = s.oneoff_value;
      if (s.recur_day !== undefined && s.recur_day !== null) day.value = String(s.recur_day);
      if (s.recur_time) time.value = s.recur_time;
      // Absent is not false. A caller that states no use_window has no opinion, so the
      // macro's own `checked` (the maintenance window) stands - otherwise the rendered
      // default would be overridden by JS on every mount and could never actually be the
      // default. An explicit false still forces exact-time, which is what keeps a cloned
      // or edited schedule winning over it (dev/changelog/830).
      if (runWindow && s.use_window !== undefined && s.use_window !== null) {
        if (s.use_window) runWindow.checked = true;
        else runTime.checked = true;
      }
      if (runWindow) syncTimeFieldVisibility();
      fire();
    },
    value: () => ({
      oneoff: dt.value, recurDay: parseInt(day.value, 10), recurTime: time.value,
      useWindow: runWindow ? runWindow.checked : false,
    }),
    payload() {
      if (mode === 'manual') return null;
      if (mode === 'recur') {
        const useWindow = runWindow ? runWindow.checked : false;
        if (useWindow) {
          return { recurring: true, recur_day: parseInt(day.value, 10), use_window: true };
        }
        if (!time.value) return { error: 'Pick a time for the recurring run.' };
        return { recurring: true, recur_day: parseInt(day.value, 10), recur_time: time.value, use_window: false };
      }
      if (!dt.value) return { error: 'Pick a date and time for the one-off run.' };
      return { recurring: false, scheduled_time: dt.value };
    },
    onChange(fn) { listeners.push(fn); },
  };
}
