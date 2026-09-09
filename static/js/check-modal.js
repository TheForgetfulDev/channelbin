/* Shared "Add a health check schedule" modal (approved design: dev/changelog/321).
   A health check is a schedule a group carries, never a second object you create
   alongside it (DESIGN-channel-groups-model.md DECIDED 2) - the copy here says so, and
   there is no longer any flow that turns one into a channel group (dev/changelog/752).
   Opened from the Groups list kebab/chip (groups.js), the unified group detail page's
   four create-check triggers (group-detail.js), the last screen of the channel search's
   group-create flow (group-create-flow.js), and the ad hoc "Test this channel" on a
   channel's own page and the search's phone row sheet - the same modal serves an existing
   channel GROUP (opts.groupId) as well as a selection with no group yet
   (opts.channelIds), where the route creates one.
   Replaces the two diverging local copies those files each carried - the list one had
   the inherited-TV-Guide-coverage notice, the detail one had never heard of inherited
   coverage, so every fix had to be made twice and had already failed to be.

   Three things about it are load-bearing and must not drift:

   1. "Set up a schedule" is the DEFAULT and the recommended path. The endpoint has
      accepted action='schedule' all along; the old modal offered only Run now / Queue,
      so the one path that keeps a group trustworthy over time was the one the UI did
      not offer. The modal therefore opens with the schedule block already showing -
      it is the initial render, never something JS corrects after open.
   2. The empty-group and busy-tester disabled states are PRESENTATION of limits the API
      enforces itself (400 'This group has no channels to test', 409 while another check
      runs). They do not replace those checks, and the submit path still handles either
      error arriving anyway from another tab or a race.
   3. Anything a profile does not set falls back to the global defaults, and the readout
      says which is which. `from_default` comes from the server (channel_tester.py::
      health_check_profile_payload) and tests `is None`, so a profile that explicitly
      sets screenshots_enabled=false is a profile value, not an unset one.

   The four helpers below the opts block are declared at FILE TOP LEVEL on purpose: they
   are pure, and tests/test_check_modal_js.py evaluates this file in node to exercise them
   directly. Nesting them inside openCreateCheckModal() would put them out of reach and
   the estimate/next-run math would go untested. Nothing test-only lives in this file.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow,
   dateToTzInputValue) and schedule-fields.js (mountScheduleFields).

   opts:
     groupId       - the channel group the new check attaches to. Omit and pass
                     channelIds instead for an ad hoc selection with no group yet
                     (the Browse tab's "Test selected") - the server creates a fresh
                     group for it, same as the old per-page flow did.
     channelIds    - ad hoc mode: the selected channel ids. Mutually exclusive with
                     groupId; exactly one of the two is supplied.
     groupName     - used in the title and the default check name (group mode only)
     modalTitle    - overrides the default `Add a health check schedule to "groupName"`
                     title (ad hoc mode always sets this - there is no group name yet)
     defaultName   - overrides the default `${groupName} - health check` Name value
                     (ignored under `nameless`, which renders no Name field at all)
     memberCount   - how many channels a run would test (0 = the empty-group state)
     profiles      - [{id, name, settings, from_default}] from health_check_profile_payload;
                     entry 0 is the id:null "Global defaults (no profile)" pseudo-profile
     profilesUrl   - Health Check Profiles page, opened in a new tab from the meta copy
     inheritedCheck- {recur_description} when the automatic TV Guide check already covers
                     this group, else null (group mode only)
     hasOwnCheck   - true when the group already has a check of its own (suppresses the
                     inherited-coverage notice: it is only news when there is nothing else)
     inGuide       - group.in_guide (group mode only)
     testerBusy    - a health check is running right now, so Run now would 409
     scheduleTemplateId / schedulePrefix - the <template> holding the rendered
                     recur_schedule_fields macro, and the id prefix it was rendered with
     allowAttachExisting - offer a "What to do" toggle between creating a new test and
                     adding these channels to an existing one (the Browse tab only - the
                     Groups pages' create-check flow always makes a new check).
     existingJobsUrl - GET endpoint returning {jobs: [...]} for the "existing test"
                     picker; defaults to /api/channel-tests/on-demand.
     initialTarget / initialJobId - pre-select "existing" mode and a job id, for the
                     `?add_to_job=<id>` deep link from a job's own "+ Add Channels" button.
     nameless      - drop the Name field entirely. A health check is a schedule its group
                     carries, so it has no name of its own; the route derives the job name
                     from the group it attaches to or creates (dev/changelog/831).
     createGroupName - ad hoc mode only: the name for the group this create will mint,
                     sent as `group_name` so it is not confused with the job's own name.
     submitLabel   - fixed label for the primary button, instead of one that changes with
                     When to run.
     onBack()      - draw a back button instead of Cancel, for a caller that opens this as
                     the last step of a longer flow.
     doneToast(action, data) - returns the success toast (HTML, so it can link at what was
                     made), for a caller whose flow created more than the check itself.
     windowSettingsUrl - where the maintenance window's hours are configured, opened in a
                     new tab from the maintenance-window line.
     panelClass    - width class for the panel, for a caller opening this as one screen of
                     a wider flow. Defaults to modal-wide as before.
     initialProfileId - pre-select this profile instead of "Global defaults" (clone-check-modal.js)
     initialSchedule - seed the schedule fields from an existing job's schedule instead of
                     the every-day/maintenance-window recurring default - same shape _schedule_ctx() /
                     G.schedule already produce ({mode, recur_day, recur_time, use_window,
                     oneoff_value}). Ignored when mode is 'manual' (nothing to seed).
     onDone()      - called after a successful create or attach; call sites reload
     onCancel()    - called if Cancel is clicked instead of submitting (clone-modal.js's
                     "both" chain: the channel group half was already created by the time
                     this screen opens, so the page still needs to refresh even if the
                     user backs out of attaching a check). Omit for today's behavior
                     (Cancel just closes, nothing else runs).
*/

const CC_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const CC_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// The tester waits BETWEEN channels, not after the last one, so it is N-1 waits.
function ccRunSeconds(n, durationSec, waitSec) {
  if (!n) return 0;
  return n * durationSec + (n - 1) * waitSec;
}

function ccDurationPhrase(seconds) {
  if (seconds < 60) return 'under a minute';
  const mins = Math.round(seconds / 60);
  const unit = (v, word) => `${v} ${word}${v === 1 ? '' : 's'}`;
  if (mins < 90) return `about ${unit(mins, 'minute')}`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `about ${unit(h, 'hour')} ${unit(m, 'minute')}` : `about ${unit(h, 'hour')}`;
}

/* Wall-clock arithmetic on the display timezone, deliberately: `nowWall` is
   'YYYY-MM-DDTHH:MM' in that zone (util.js::dateToTzInputValue), and everything below is
   plain calendar math on those parts. Doing it on Date instants instead would evaluate
   "has this time already passed" in the browser's zone, which is the exact bug the
   spec calls out. recur_day: 0 = every day, 1 = Sunday ... 7 = Saturday. */
function ccNextRun(recurDay, timeHHMM, nowWall) {
  if (!timeHHMM) return null;
  const [dPart, tPart] = nowWall.split('T');
  const [y, mo, d] = dPart.split('-').map(Number);
  const base = new Date(Date.UTC(y, mo - 1, d));
  const laterToday = timeHHMM > tPart;
  let delta;
  if (!recurDay) {
    delta = laterToday ? 0 : 1;
  } else {
    delta = ((recurDay - 1) - base.getUTCDay() + 7) % 7;
    if (delta === 0 && !laterToday) delta = 7;
  }
  const target = new Date(base.getTime() + delta * 86400000);
  return {
    dow: target.getUTCDay(),
    year: target.getUTCFullYear(),
    month: target.getUTCMonth() + 1,
    day: target.getUTCDate(),
    time: timeHHMM,
  };
}

function ccClock(timeHHMM, hour12) {
  const [h, m] = timeHHMM.split(':').map(Number);
  if (!hour12) return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
  const suffix = h < 12 ? 'AM' : 'PM';
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, '0')} ${suffix}`;
}

function ccWhenLabel(parts, hour12) {
  return `${CC_WEEKDAYS[parts.dow]}, ${CC_MONTHS[parts.month - 1]} ${parts.day} at ${ccClock(parts.time, hour12)}`;
}

/* Why the maintenance window is the recommended answer, plus the one number that is ever
   news: that this day's window cannot fit what is already booked into it. `plan` is
   GET /api/channel-tests/window-plan's response; recurDay is 0 (every day) - 7 (Saturday),
   the same OnDemandTestJob.recur_day encoding used everywhere else in this file.

   What this replaced stated the capacity arithmetic unconditionally - window length, the
   day's booked total, what this check adds - which read as a puzzle rather than as help:
   the numbers are in units nobody chose, about a day nobody asked about, and a healthy
   window ended up reading like a problem. The over-capacity half survives because that one
   IS news, and only when it is true (dev/changelog/831).

   Purely informational - CLAUDE.md "enforcement lives server-side": the server does not cap
   capacity either, so this never blocks the create button, only colors the line. */
function ccWindowRecommendLine(plan, recurDay, addSeconds, settingsUrl) {
  const day = plan.days[recurDay] || { total_seconds: 0 };
  const projected = day.total_seconds + addSeconds;
  let html = '<strong>Recommended.</strong> Assigning tests to the maintenance window allows ' +
    'ChannelBin to control when tests run so it can avoid overlapping tasks. Your maintenance ' +
    `window hours can be configured in <a href="${escHtml(settingsUrl || '#')}" target="_blank" ` +
    'rel="noopener">settings</a>.';
  if (projected > plan.window_seconds) {
    html += ' <span style="color:var(--warn)">This may not all finish: the window is ' +
      `${escHtml(ccDurationPhrase(plan.window_seconds))} long and this day would hold ` +
      `${escHtml(ccDurationPhrase(projected))} of checks. Widen the window, use a faster ` +
      'profile, or move this check to another day.</span>';
  }
  return html;
}

/* The "add to an existing test" picker's eligible-jobs filter, pulled out as a pure
   function so it is directly testable (see check-modal.js's file-top-level note above).
   Two rules, both traced to real bugs in the two flows this modal replaces: the pinned
   system "TV Guide Channels" check tests whatever is in the guide and its membership
   can't be edited (app/routes/channel_tests.py::_editable_job_group_or_error already
   enforces this server-side - is_system is filtered here only so the picker doesn't
   offer a choice that then 400s), and a job with zero channels must still appear (it's
   unreachable otherwise until it somehow gains a channel some other way). Only RUNNING
   is excluded for a different reason - the job is mid-test and its membership route
   itself 409s while running. */
function ccEligibleJobs(jobs) {
  return (jobs || []).filter(j => !j.is_system && j.status !== 'RUNNING');
}

/* The POST body for /api/channel-tests/on-demand. profile_id is null and never '' -
   the route reads `profile_id_raw not in (None, '')`, but a bare '' travelling as a
   string is the kind of thing that quietly becomes a 0 somewhere later. `schedule` is
   mountScheduleFields().payload() and is merged verbatim, so the recurring vs one-off
   shape has exactly one author. Exactly one of groupId/channelIds is set (ad hoc mode
   has no group yet - the route creates one, same as attach_group_id makes it reuse one). */
function ccPayload(o) {
  const body = {
    action: o.action,
    profile_id: o.profileId === '' || o.profileId === undefined ? null : o.profileId,
  };
  // Omitted entirely in nameless mode: the route derives the job's name from the group it
  // is attached to or about to create, and sending '' would be a name it has to reject.
  if (o.name) body.name = o.name;
  if (o.groupId != null) body.attach_group_id = o.groupId;
  else body.channel_ids = o.channelIds;
  // The group this request creates around the selection, named by the user. Distinct from
  // `name`, which is the JOB's - the ad hoc path used one string for both, so a nameless
  // caller would otherwise have created a group called "X - health check".
  if (o.groupId == null && o.groupName) body.group_name = o.groupName;
  if (o.action === 'schedule' && o.schedule) Object.assign(body, o.schedule);
  return body;
}

function openCreateCheckModal(opts) {
  const profiles = opts.profiles || [];
  const n = opts.memberCount || 0;
  const empty = n === 0;
  const busy = !!opts.testerBusy;
  const hour12 = displayHour12();
  const onDone = opts.onDone || (() => window.location.reload());

  // `submitLabel` fixes the primary button for a caller whose Create finishes a whole
  // flow whatever When to run says - a button that renames itself under the user is fine
  // as a standalone dialog's own label and wrong as the last step of three.
  const ACTIONS = {
    schedule: { label: opts.submitLabel || 'Create schedule', option: 'Set up a schedule' },
    start: { label: opts.submitLabel || 'Create and run', option: 'Run now' },
    queue: { label: opts.submitLabel || 'Do not schedule', option: 'Do not schedule' },
  };
  const seedSchedule = opts.initialSchedule && opts.initialSchedule.mode !== 'manual' ? opts.initialSchedule : null;
  const state = {
    profileId: opts.initialProfileId == null ? '' : String(opts.initialProfileId),
    action: 'schedule',
    repeat: seedSchedule && seedSchedule.mode === 'once' ? 'once' : 'recur',
    target: opts.initialTarget || 'new',
  };
  const defaultCheckName = opts.defaultName || `${opts.groupName} - health check`;

  const body = document.createElement('div');
  let createBtn = null;
  let sched = null;
  let jobsLoaded = false;
  // Fetched once per modal open and cached - the plan doesn't change while the modal is
  // open, so a live readout re-render (profile change, day change) must not re-fetch.
  let windowPlan = null;
  let windowPlanPromise = null;
  function loadWindowPlan() {
    if (!windowPlanPromise) windowPlanPromise = jsonFetch('/api/channel-tests/window-plan').catch(() => null);
    return windowPlanPromise;
  }

  function profileFor(id) {
    const key = id === '' || id === null ? null : Number(id);
    return profiles.find(p => p.id === key) || profiles[0] || null;
  }

  // "What this will do": one row per setting, with a grey `default` pill on any value
  // that fell through to the global defaults. The connect row folds three fields into
  // one sentence, so it only claims "default" when all three of them are.
  function readoutHtml() {
    const p = profileFor(state.profileId);
    if (!p) return '';
    const s = p.settings;
    const from = new Set(p.from_default || []);
    const pill = (...fields) => (fields.every(f => from.has(f))
      ? ' <span class="pfx-def">default</span>' : '');
    const rows = [
      ['Watches each channel for', `${s.test_duration_seconds}s`, pill('test_duration_seconds')],
      ['Waits between channels', `${s.wait_between_channels_seconds}s`, pill('wait_between_channels_seconds')],
      ['Screenshot of each channel', s.screenshots_enabled ? 'Yes' : 'No', pill('screenshots_enabled')],
      ['Connect attempts',
        `${s.connect_retries + 1} tries, ${s.connect_timeout_seconds}s timeout, ${s.connect_retry_delay_seconds}s apart`,
        pill('connect_retries', 'connect_timeout_seconds', 'connect_retry_delay_seconds')],
    ];
    const closing = empty
      ? 'This group has no channels, so there is nothing to run.'
      : `One run over ${n} channel${n === 1 ? '' : 's'} takes <strong>` +
        `${escHtml(ccDurationPhrase(ccRunSeconds(n, s.test_duration_seconds, s.wait_between_channels_seconds)))}</strong>.`;
    return '<div class="pfx">' +
      rows.map(([k, v, tag]) => `<div class="pfx-k">${k}</div><div class="pfx-v">${escHtml(v)}${tag}</div>`).join('') +
      `</div><div class="pfx-sum">${closing}</div>`;
  }

  function firstRunHtml() {
    const nowWall = dateToTzInputValue(new Date(), displayTz());
    if (state.repeat === 'once') {
      const v = sched ? sched.value().oneoff : '';
      if (!v) return 'Pick the date and time it should run.';
      if (v <= nowWall) return '<strong>That time has already passed - pick a future one.</strong>';
      const [dPart, tPart] = v.split('T');
      const [y, mo, d] = dPart.split('-').map(Number);
      const parts = { dow: new Date(Date.UTC(y, mo - 1, d)).getUTCDay(), year: y, month: mo, day: d, time: tPart };
      return `First run: <strong>${escHtml(ccWhenLabel(parts, hour12))}</strong>, then nothing further.`;
    }
    const v = sched ? sched.value() : null;
    if (v && v.useWindow) {
      if (!windowPlan) {
        loadWindowPlan().then((plan) => { windowPlan = plan; paintFirstRun(); });
        return 'Checking maintenance window capacity...';
      }
      const p = profileFor(state.profileId);
      const addSeconds = p ? ccRunSeconds(n, p.settings.test_duration_seconds, p.settings.wait_between_channels_seconds) : 0;
      return ccWindowRecommendLine(windowPlan, v.recurDay, addSeconds, opts.windowSettingsUrl);
    }
    if (!v || !v.recurTime) return 'Pick the day and time it should run.';
    const parts = ccNextRun(v.recurDay, v.recurTime, nowWall);
    const every = v.recurDay ? `every ${CC_WEEKDAYS[parts.dow]}` : 'every day';
    return `First run: <strong>${escHtml(ccWhenLabel(parts, hour12))}</strong>, then ${every} at the same time.`;
  }

  /* Only the SELECTED option describes itself. Three descriptions is three times the
     reading for one decision and two thirds of it describes roads not taken. The busy
     line is the exception and is always on: "Run now" is DISABLED while another check
     runs, so a reason that only rendered once it was selected would be a reason nobody
     could ever reach. */
  const WHEN_META = {
    schedule: 'Run the check automatically on a recurring schedule, or at a future date/time.',
    start: 'Starts a test immediately, only runs once. You can add a schedule later from the ' +
      'group\'s page.',
    queue: 'The channel group is created but no tests are run. Can be scheduled or run manually ' +
      'at any time from the group\'s page.',
  };
  const optionMetaInner = () => `<div class="opt-line">${WHEN_META[state.action]}</div>` + (busy
    ? '<div class="opt-line"><strong>Run now is unavailable</strong>Another health check is ' +
      'running right now. Wait for it to finish, or pick one of the other options.</div>'
    : '');
  const optionMeta = () => `<div id="cc-when-meta">${optionMetaInner()}</div>`;

  const notices = [
    opts.groupId != null
      ? '<div class="notice notice-info">These settings allow you to create a scheduled health check on ' +
        'your group. In addition to verifying if the channels work and gathering details such as bitrate and ' +
        'resolution, this is the primary way that health scores for your channels are calculated. That is what ' +
        'keeps the group healthy and functional - it catches a channel that stopped working, dropped ' +
        'resolution, or has constant stalling issues, before a recording lands on it.</div>'
      : '<div class="notice notice-info">A health check monitors the selected channels on a schedule. It ' +
        'catches a feed that stopped playing, dropped resolution or changed frame rate, before a recording ' +
        'lands on it.</div>',
  ];
  // Not gated on opts.inGuide: the automatic check's scheduleless-group fallback covers
  // groups that are not in the guide at all (dev/changelog/752). The server sets
  // inheritedCheck only when this group really is in its target set.
  if (opts.inheritedCheck && !opts.hasOwnCheck) {
    notices.push('<div class="notice notice-info">The automatic TV Guide health check ' +
      `(${escHtml(opts.inheritedCheck.recur_description || 'on a recurring schedule')}) tests one channel per ` +
      'guide row, so it covers the member serving this group and none of its others. Give the group its own ' +
      'schedule to have every member re-tested.</div>');
  }
  if (empty) {
    notices.push('<div class="notice notice-warn">This group has no channels yet, so there is nothing to test. ' +
      'Add channels to it first, then come back and create the check.</div>');
  }

  const profileOptions = profiles.map((p) => {
    const val = p.id === null ? '' : p.id;
    const sel = String(val) === state.profileId ? ' selected' : '';
    return `<option value="${val}"${sel}>${escHtml(p.name)}</option>`;
  }).join('');

  let targetBlock = '';
  if (opts.allowAttachExisting) {
    targetBlock = '<fieldset class="gd-fset"><div class="gd-fset-head">What to do</div>' +
      fieldRow({ label: 'Add to', meta: 'Create a brand new test, or add these channels to a test that ' +
        'already exists (its own schedule and profile are unchanged).',
        control: '<select id="cc-target">' +
          `<option value="new"${state.target === 'new' ? ' selected' : ''}>Create a new test</option>` +
          `<option value="existing"${state.target === 'existing' ? ' selected' : ''}>Add to an existing test</option>` +
          '</select>' }) +
      '<div class="gd-field full" id="cc-attach-block" style="display:none">' +
      fieldRow({ label: 'Existing test', meta: 'Only tests that are not currently running can be picked.',
        control: '<select id="cc-existing-job"><option>Loading…</option></select>' }) +
      '</div></fieldset>';
  }

  // When to run leads: it is the decision this screen exists to take, and it governs
  // whether the rest of the screen is even shown.
  body.innerHTML = notices.join('') + targetBlock +
    '<div id="cc-create-block">' +
    '<fieldset class="gd-fset"><div class="gd-fset-head">When to run</div>' +
    fieldRow({ label: 'When to run', wide: true, meta: optionMeta(),
      control: '<select id="cc-when">' +
        '<option value="schedule" selected>Set up a schedule (recommended)</option>' +
        `<option value="start"${busy ? ' disabled' : ''}>Run now${busy ? ' (unavailable)' : ''}</option>` +
        '<option value="queue">Do not schedule</option></select>' }) +
    fieldRow({ id: 'repeat', sub: true, label: 'Repeat?',
      meta: 'A recurring check runs repeatedly at the selected day(s) and time. A one time check runs ' +
        'once at your selected date/time.',
      control: '<select id="cc-repeat"><option value="recur" selected>Recurring (recommended)</option>' +
        '<option value="once">One time</option></select>' }) +
    '<div class="gd-field full sub" data-frow="schedule"><div id="cc-sched"></div>' +
    '<div class="pfx-sum" id="cc-firstrun"></div></div>' +
    '</fieldset>' +
    '<fieldset class="gd-fset"><div class="gd-fset-head">Health check</div>' +
    (opts.nameless ? '' : fieldRow({ label: 'Name', wide: true,
      meta: 'How it shows up in the health checks list, in alerts and in notifications.',
      control: `<input type="text" id="cc-name" value="${escHtml(defaultCheckName)}">` })) +
    fieldRow({ label: 'Profile',
      meta: 'Profiles control how long each channel is monitored for during a test, and how hard the test ' +
        'tries to (re)connect if needed. Unset options fall back to your global defaults. Profiles ' +
        `are created and edited under <a href="${escHtml(opts.profilesUrl || '#')}" target="_blank" ` +
        'rel="noopener">Health Check Profiles</a>.',
      control: `<select id="cc-profile">${profileOptions}</select>` }) +
    fieldRow({ full: true, label: 'What this will do', meta: `<div id="cc-readout">${readoutHtml()}</div>` }) +
    '</fieldset>' +
    '</div>';

  sched = mountScheduleFields({
    host: body.querySelector('#cc-sched'),
    templateId: opts.scheduleTemplateId,
    prefix: opts.schedulePrefix,
  });
  // A recurring check needs a day and a time to be worth recommending, so the modal
  // opens on a real one rather than an empty input the user must discover - or, when a
  // source job's schedule was passed in (clone-check-modal.js), that one instead.
  // The default is every day in the maintenance window (dev/changelog/830): recur_time
  // is still seeded so switching to "a specific time" lands on a real hour rather than
  // an empty input.
  if (seedSchedule) {
    sched.prefill({
      oneoff_value: seedSchedule.oneoff_value,
      recur_day: seedSchedule.recur_day,
      recur_time: seedSchedule.recur_time,
      use_window: seedSchedule.use_window,
    });
    sched.setMode(state.repeat);
  } else {
    sched.prefill({ recur_day: 0, recur_time: '03:00', use_window: true });
    sched.setMode('recur');
  }

  const readoutEl = body.querySelector('#cc-readout');
  const whenMetaEl = body.querySelector('#cc-when-meta');
  const firstRunEl = body.querySelector('#cc-firstrun');
  const scheduleRows = [body.querySelector('[data-frow="repeat"]'), body.querySelector('[data-frow="schedule"]')];
  const createBlock = body.querySelector('#cc-create-block');
  const attachBlock = body.querySelector('#cc-attach-block');

  // One updater per region: the readout follows the profile, the first-run line follows
  // the schedule inputs, and the When-to-run select owns the block visibility and the
  // button label. Nothing writes another region's node.
  const paintFirstRun = () => { firstRunEl.innerHTML = firstRunHtml(); };
  sched.onChange(paintFirstRun);

  function paintWhen() {
    const scheduling = state.action === 'schedule';
    if (whenMetaEl) whenMetaEl.innerHTML = optionMetaInner();
    scheduleRows.forEach(el => { el.style.display = scheduling ? '' : 'none'; });
    if (createBtn && state.target !== 'existing') createBtn.textContent = ACTIONS[state.action].label;
    if (scheduling) paintFirstRun();
  }

  function loadExistingJobs() {
    jobsLoaded = true;
    const select = body.querySelector('#cc-existing-job');
    select.innerHTML = '<option>Loading…</option>';
    jsonFetch(opts.existingJobsUrl || '/api/channel-tests/on-demand').then(data => {
      const jobs = ccEligibleJobs(data.jobs);
      if (!jobs.length) {
        select.innerHTML = '<option value="">No eligible tests - create one instead</option>';
        return;
      }
      select.innerHTML = jobs.map(j => `<option value="${j.id}">${escHtml(j.name)} ` +
        `(${j.status}, ${j.channel_count} channel${j.channel_count === 1 ? '' : 's'})</option>`).join('');
      if (opts.initialJobId && jobs.some(j => String(j.id) === String(opts.initialJobId))) {
        select.value = opts.initialJobId;
      }
    }).catch(() => { select.innerHTML = '<option value="">Failed to load tests</option>'; });
  }

  // The whole create fieldset (name/profile/when-to-run) is one region; the attach-
  // existing picker is another. Only one is ever visible - there is nothing left in
  // "create" mode to also apply when attaching to an existing test, since the existing
  // job keeps its own profile and schedule untouched.
  function paintTarget() {
    if (!createBlock) return;
    const existing = state.target === 'existing';
    createBlock.style.display = existing ? 'none' : '';
    attachBlock.style.display = existing ? '' : 'none';
    if (createBtn) createBtn.textContent = existing ? 'Add to test' : ACTIONS[state.action].label;
    if (existing && !jobsLoaded) loadExistingJobs();
  }

  body.addEventListener('change', (e) => {
    if (e.target.id === 'cc-target') { state.target = e.target.value; paintTarget(); return; }
    if (e.target.id === 'cc-profile') {
      state.profileId = e.target.value;
      readoutEl.innerHTML = readoutHtml();
      // The window-mode capacity line's "this check adds" depends on the profile's
      // duration/wait too - the exact-time first-run text doesn't, but paintFirstRun()
      // is a no-op there since firstRunHtml() ignores the profile in that branch.
      paintFirstRun();
      return;
    }
    if (e.target.id === 'cc-when') { state.action = e.target.value; paintWhen(); return; }
    if (e.target.id === 'cc-repeat') {
      state.repeat = e.target.value;
      sched.setMode(state.repeat);
    }
  });

  function submitAttachExisting(close) {
    const jobId = body.querySelector('#cc-existing-job').value;
    if (!jobId) { showToast('Choose a test to add these channels to.', { type: 'error' }); return; }
    jsonFetch(`/api/channel-tests/on-demand/${jobId}/channels`, {
      method: 'POST',
      body: JSON.stringify({ channel_ids: opts.channelIds }),
    }).then((data) => {
      showToast(`${data.added_count || opts.channelIds.length} channel(s) added to the test.`);
      close();
      onDone();
    }).catch(err => showToast(err.message, { type: 'error' }));
  }

  function submit(close) {
    if (state.target === 'existing') { submitAttachExisting(close); return; }
    // In nameless mode there is no input to read and the server derives the job name from
    // the group, so `name` is left out of the payload entirely rather than guessed at here.
    const nameEl = body.querySelector('#cc-name');
    const name = nameEl ? nameEl.value.trim() : '';
    if (nameEl && !name) { showToast('Give the health check a name.', { type: 'error' }); return; }
    let schedule = null;
    if (state.action === 'schedule') {
      schedule = sched.payload();
      if (schedule && schedule.error) { showToast(schedule.error, { type: 'error' }); return; }
    }
    const profileId = state.profileId === '' ? null : Number(state.profileId);
    jsonFetch('/api/channel-tests/on-demand', {
      method: 'POST',
      body: JSON.stringify(ccPayload({
        name, action: state.action, groupId: opts.groupId, channelIds: opts.channelIds,
        groupName: opts.createGroupName, profileId, schedule,
      })),
    }).then((data) => {
      // The response is threaded in because a caller whose flow created more than the
      // check itself has to be able to link at the id this same request minted.
      if (opts.doneToast) showToast(opts.doneToast(state.action, data), { html: true, durationMs: 12000 });
      else showToast(`Health check "${name || data.name}" created.`);
      close();
      onDone();
    }).catch(err => showToast(err.message, { type: 'error' }));
  }

  const modal = buildModal({
    title: opts.modalTitle || `Add a health check schedule to "${opts.groupName}"`,
    panelClass: opts.panelClass || 'modal-wide',
    body,
    footer: [
      // A caller that opens this as the last step of a longer flow gets a way BACK rather
      // than a fourth way to dismiss - the arrow is what marks it as navigation
      // (CLAUDE.md UI naming). Everything else keeps today's Cancel.
      opts.onBack
        ? { label: '← Back', class: 'btn', onClick: (close) => { close(); opts.onBack(); return false; } }
        : { label: 'Cancel', class: 'btn', onClick: (close) => { close(); if (opts.onCancel) opts.onCancel(); } },
      { label: ACTIONS.schedule.label, class: 'btn btn-primary', onClick: (close) => { submit(close); return false; } },
    ],
  });
  createBtn = modal.querySelector('.modal-foot .btn-primary');
  if (empty) {
    createBtn.disabled = true;
    createBtn.title = 'Add channels to this group first';
  }
  paintWhen();
  if (opts.allowAttachExisting) paintTarget();
  return modal;
}
