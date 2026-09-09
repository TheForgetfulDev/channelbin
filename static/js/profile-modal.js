/* Shared create/edit modal for the app's profile types (Health Check Profiles now,
   Recording Profiles in the same rollout). Replaces the two standalone
   `/…/new` + `/…/edit` form pages, which were the last surfaces still on the
   pre-redesign `.page-header` / `.form-card` chrome.

   Both profile types are the same form in different clothes: a name, then a list of
   "leave blank to inherit the global default" overrides. So the modal is spec-driven -
   a section list describes the fields, and everything below is type-driven from it.
   Adding the second type is a spec, not a second modal.

   Two things about it are load-bearing and must not drift:

   1. **Unset is not zero.** A blank numeric field means "inherit the global default"
      and travels as `null`; `0` is a real value the user chose. The parse path keeps
      those distinct end to end (pmPayload -> the API's _read_profile_body), because
      collapsing them silently rewrites a profile's meaning - a blank Test duration
      would become "test each channel for 0 seconds".
   2. **The client-side checks are presentation, not enforcement.** The API runs the
      same validation and is what actually protects the row (CLAUDE.md: enforcement
      lives server-side). Failing to submit here is a courtesy; the submit path still
      handles the error arriving from the server anyway.

   The pure helpers below the spec block are declared at FILE TOP LEVEL on purpose, the
   same way check-modal.js's are: tests/test_profile_modal_js.py evaluates this file in
   node and calls them directly. Nothing test-only lives here - no module.exports tail,
   no injected globals.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow).
*/

/* Field spec entry:
     key         - the API field name, and the input's id suffix
     label       - on-screen label; also what a validation error names
     meta        - the explanatory line under the label
     type        - 'text' | 'int' | 'tristate'
     required    - text only; blocks submit when empty
     inheritable - blank/unset falls back to the global default (adds the hint sentence)
     unit        - appended to the default value in the hint ('s', 'm', …)
     trueLabel / falseLabel - tristate option labels, also used in its default hint
     wide        - render the control in the wider column (long free-text values) */
const HEALTH_CHECK_PROFILE_SECTIONS = [
  {
    title: 'Profile',
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, wide: true,
        placeholder: 'e.g. Quick Check, Deep Scan, Flaky Account',
        meta: 'A friendly label shown when picking a profile for a health check.' },
    ],
  },
  {
    title: 'Testing',
    fields: [
      { key: 'test_duration_seconds', label: 'Test duration', type: 'int',
        inheritable: true, unit: 's',
        meta: 'How long each channel is watched before the test moves on.' },
      { key: 'wait_between_channels_seconds', label: 'Wait between channels', type: 'int',
        inheritable: true, unit: 's',
        meta: 'Pause after finishing one channel before the next one starts.' },
      { key: 'screenshots_enabled', label: 'Capture screenshots', type: 'tristate',
        inheritable: true, trueLabel: 'Always capture', falseLabel: 'Never capture',
        meta: 'Grab a still frame during each channel test.' },
    ],
  },
  {
    title: 'Connecting',
    fields: [
      { key: 'connect_retries', label: 'Connect retries', type: 'int', inheritable: true,
        meta: 'Extra connection attempts after the first one fails.' },
      { key: 'connect_timeout_seconds', label: 'Connect timeout', type: 'int',
        inheritable: true, unit: 's',
        meta: 'How long to wait for the first byte of data per attempt.' },
      { key: 'connect_retry_delay_seconds', label: 'Retry delay', type: 'int',
        inheritable: true, unit: 's',
        meta: 'How long to wait between connection attempts.' },
    ],
  },
];

/* Recording Profiles. Two shapes here differ from the health-check spec above and both
   are deliberate (they mirror ProfileField declarations in app/routes/profiles.py):

   - The padding fields are NOT inheritable and carry blankValue 0 - their columns are
     NOT NULL, so clearing one stores 0 rather than "inherit".
   - `retention_days` IS inheritable, and its 0 is not its blank: blank inherits the
     global retention window, 0 means never auto-delete even when a global window is set.
     Those are opposite outcomes, which is why the copy spells both out.

   `filename_template` stays a plain text input on purpose. The template designer in
   Settings is itself being revamped, and this field is meant to get that revamped
   version rather than the current one - doing it now would mean building it twice
   (2026-07-27; tracked in the backlog against the filename-template item). */
const RECORDING_PROFILE_SECTIONS = [
  {
    title: 'Profile',
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, wide: true,
        placeholder: 'e.g. Sports, Movies, Flaky Stream',
        meta: 'A friendly label shown when picking a profile for a recording.' },
      { key: 'filename_template', label: 'Filename template', type: 'text',
        inheritable: true, wide: true,
        meta: 'Names the finished file for recordings using this profile. Supports ' +
          '{date}, {title}, {sub_title}, {description}, {channel}, {category}, ' +
          '{start_time} and {end_time}.' },
    ],
  },
  {
    title: 'Padding',
    fields: [
      { key: 'pre_padding_minutes', label: 'Pre-record padding', type: 'int', blankValue: 0,
        meta: 'Starts this many minutes early. Applies only to recordings scheduled from ' +
          'a TV Guide program, and only as a starting suggestion - you can still edit the ' +
          'times before submitting. Blank counts as 0.' },
      { key: 'post_padding_minutes', label: 'Post-record padding', type: 'int', blankValue: 0,
        meta: 'Keeps recording this many minutes past the scheduled end, under the same ' +
          'terms as pre-record padding. Blank counts as 0.' },
    ],
  },
  {
    title: 'When a stream drops',
    fields: [
      { key: 'stall_timeout_seconds', label: 'Stall timeout', type: 'int',
        inheritable: true, unit: 's',
        meta: 'How long the file can stop growing before that counts as a stall.' },
      { key: 'restart_delay_seconds', label: 'Restart delay', type: 'int',
        inheritable: true, unit: 's',
        meta: 'How long to wait before restarting ffmpeg after a stall.' },
      { key: 'max_consecutive_failures', label: 'Max consecutive failures', type: 'int',
        inheritable: true,
        meta: 'How many restarts in a row may fail before the recording is given up on ' +
          'and marked FAILED.' },
    ],
  },
  {
    title: 'Housekeeping',
    fields: [
      { key: 'retention_days', label: 'Auto-delete after', type: 'int',
        inheritable: true, unit: ' days',
        meta: 'Deletes recordings made with this profile, and their files, once they are ' +
          'this old. Enter 0 to keep them forever even when a global retention window is ' +
          'set - that is different from leaving it blank, which follows the global.' },
      { key: 'pre_check_enabled', label: 'Pre-recording health check', type: 'tristate',
        inheritable: true, trueLabel: 'Enabled', falseLabel: 'Disabled',
        meta: 'Tests the channel shortly before the recording starts, so a dead feed is ' +
          'caught while there is still time to do something about it.' },
    ],
  },
];

function pmFlatFields(sections) {
  return sections.reduce((acc, s) => acc.concat(s.fields), []);
}

/* The "blank = inherit" sentence appended to an inheritable field's meta. Kept pure and
   separate because it is the one piece of copy that has to stay true to the server's
   resolved defaults - it renders whatever `defaults` actually says rather than a literal
   repeated in the template (app/routes/health_check_profiles.py hands over
   resolve_health_check_settings(ct_cfg, None), the single source for those fallbacks). */
function pmHintText(f, defaults) {
  if (!f.inheritable) return '';
  const d = (defaults || {})[f.key];
  if (d === undefined || d === null) return 'Leave blank to use the global default.';
  if (f.type === 'tristate') {
    return `Leave unset to use the global default (${d ? f.trueLabel : f.falseLabel}).`;
  }
  // A text default (a filename template) can be long and the placeholder already shows
  // it verbatim - quoting it here too just prints the same string twice in one row.
  if (f.type === 'text') return 'Leave blank to use the global default.';
  return `Leave blank to use the global default (${d}${f.unit || ''}).`;
}

/* First failing field's message, or null. Mirrors the API's _read_profile_body so the
   two agree on what "a non-negative whole number" means - notably that an empty numeric
   field is valid (it means inherit) while `-1` and `1.5` are not. */
function pmValidate(fields, values) {
  for (const f of fields) {
    const v = values[f.key];
    if (f.type === 'text') {
      if (f.required && !String(v == null ? '' : v).trim()) return `${f.label} is required.`;
    } else if (f.type === 'int') {
      const raw = v == null ? '' : String(v).trim();
      if (raw === '') continue;
      if (!/^\d+$/.test(raw)) return `${f.label} must be a non-negative whole number.`;
    }
  }
  return null;
}

/* The request body. Every field is always present - an omitted key and an explicit null
   would both read as "unset" to the API, but only sending all of them makes an edit that
   CLEARS a value work (the update path assigns every field it is given). */
function pmPayload(fields, values) {
  const body = {};
  for (const f of fields) {
    const v = values[f.key];
    // `blankValue` is what an emptied control stores. It defaults to null (= inherit the
    // global default); a field whose column is NOT NULL declares its own, which is why
    // clearing a padding box stores 0 rather than trying to write NULL. Mirrors
    // ProfileField.blank_value in app/profile_forms.py.
    const blank = f.blankValue === undefined ? null : f.blankValue;
    if (f.type === 'text') {
      const raw = String(v == null ? '' : v).trim();
      body[f.key] = (f.inheritable && raw === '') ? blank : raw;
    } else if (f.type === 'int') {
      const raw = v == null ? '' : String(v).trim();
      body[f.key] = raw === '' ? blank : Number(raw);
    } else if (f.type === 'tristate') {
      const raw = v == null ? '' : String(v);
      body[f.key] = raw === '' ? blank : raw === 'true';
    }
  }
  return body;
}

function pmControlHtml(f, value, defaults) {
  const id = `pm-${f.key}`;
  if (f.type === 'tristate') {
    const d = (defaults || {})[f.key];
    const dLabel = d === undefined || d === null
      ? 'Default' : `Default (${d ? f.trueLabel : f.falseLabel})`;
    const sel = (v) => (String(value == null ? '' : value) === v ? ' selected' : '');
    return `<select id="${id}">` +
      `<option value=""${sel('')}>${escHtml(dLabel)}</option>` +
      `<option value="true"${sel('true')}>${escHtml(f.trueLabel)}</option>` +
      `<option value="false"${sel('false')}>${escHtml(f.falseLabel)}</option>` +
      '</select>';
  }
  const v = value == null ? '' : String(value);
  const d = (defaults || {})[f.key];
  const hasDefault = d !== undefined && d !== null;
  if (f.type === 'int') {
    // A non-inheritable number inherits nothing, so "Default" would be a lie - its
    // placeholder shows the value clearing it actually stores (the padding fields' 0).
    const blank = f.blankValue === undefined ? null : f.blankValue;
    const ph = f.inheritable
      ? (hasDefault ? `Default (${d})` : 'Default')
      : (blank === null ? '' : String(blank));
    return `<input type="number" id="${id}" min="0" step="1" value="${escHtml(v)}" ` +
      `placeholder="${escHtml(ph)}">`;
  }
  const ph = (f.inheritable && hasDefault) ? `Default (${d})` : (f.placeholder || '');
  return `<input type="text" id="${id}" value="${escHtml(v)}" ` +
    `placeholder="${escHtml(ph)}">`;
}

function pmSectionsHtml(sections, values, defaults) {
  return sections.map((s) => {
    const rows = s.fields.map((f) => {
      const hint = pmHintText(f, defaults);
      return fieldRow({
        label: escHtml(f.label),
        wide: f.wide,
        meta: escHtml(f.meta) + (hint ? ` ${escHtml(hint)}` : ''),
        control: pmControlHtml(f, values[f.key], defaults),
      });
    }).join('');
    return `<fieldset class="gd-fset"><div class="gd-fset-head">${escHtml(s.title)}</div>${rows}</fieldset>`;
  }).join('');
}

/* opts:
     title        - modal title
     sections     - field spec (HEALTH_CHECK_PROFILE_SECTIONS, …)
     values       - current values keyed by field key; {} for a new profile
     defaults     - resolved global defaults, for the hints and placeholders
     notice       - optional HTML notice rendered above the fields
     submitUrl / method / submitLabel
     onDone()     - called after a successful save; call sites reload */
function openProfileModal(opts) {
  const sections = opts.sections;
  const fields = pmFlatFields(sections);
  const values = Object.assign({}, opts.values || {});
  const onDone = opts.onDone || (() => window.location.reload());

  const body = document.createElement('div');
  body.innerHTML = (opts.notice || '') + pmSectionsHtml(sections, values, opts.defaults);

  const read = () => {
    const out = {};
    fields.forEach((f) => {
      const el = body.querySelector(`#pm-${f.key}`);
      out[f.key] = el ? el.value : null;
    });
    return out;
  };

  function submit(close) {
    const current = read();
    const error = pmValidate(fields, current);
    if (error) { showToast(error, { type: 'error' }); return; }
    jsonFetch(opts.submitUrl, {
      method: opts.method,
      body: JSON.stringify(pmPayload(fields, current)),
    }).then(() => {
      showToast(`Profile "${pmPayload(fields, current).name}" saved.`);
      close();
      onDone();
    }).catch((err) => showToast(err.message, { type: 'error' }));
  }

  const modal = buildModal({
    title: opts.title,
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: opts.submitLabel, class: 'btn btn-primary',
        onClick: (close) => { submit(close); return false; } },
    ],
  });
  const first = body.querySelector('input, select');
  if (first) first.focus();
  return modal;
}

/* Health Check Profiles list page (templates/health_check_profiles.html).
   opts: { profile, defaults, onDone } - profile omitted for the create flow. */
function openHealthCheckProfileModal(opts) {
  const p = opts.profile || null;
  return openProfileModal({
    title: p ? `Edit "${p.name}"` : 'Add a health check profile',
    sections: HEALTH_CHECK_PROFILE_SECTIONS,
    values: p || {},
    defaults: opts.defaults,
    notice: p
      ? '<div class="notice notice-info">Changes apply to every health check using this ' +
        'profile, including one that is queued, scheduled, or running right now.</div>'
      : '<div class="notice notice-info">A profile bundles the settings a health check runs ' +
        'with. Anything you leave blank falls back to your global defaults, so a profile only ' +
        'has to spell out what it changes.</div>',
    submitUrl: p ? `/api/health-check-profiles/${p.id}` : '/api/health-check-profiles',
    method: p ? 'PUT' : 'POST',
    submitLabel: p ? 'Save' : 'Add profile',
    onDone: opts.onDone,
  });
}

/* Recording Profiles list page (templates/profiles.html).
   opts: { profile, defaults, onDone } - profile omitted for the create flow. */
function openRecordingProfileModal(opts) {
  const p = opts.profile || null;
  return openProfileModal({
    title: p ? `Edit "${p.name}"` : 'Add a recording profile',
    sections: RECORDING_PROFILE_SECTIONS,
    values: p || {},
    defaults: opts.defaults,
    notice: p
      ? '<div class="notice notice-info">Changes apply to every recording using this profile, ' +
        'including one that is scheduled or running right now, and to any channel that has it ' +
        'set as its default.</div>'
      : '<div class="notice notice-info">A profile bundles the settings a recording runs with, ' +
        'so you can pick them by name when scheduling - or set one as a channel\'s default. ' +
        'Anything you leave blank falls back to your global settings.</div>',
    submitUrl: p ? `/api/profiles/${p.id}` : '/api/profiles',
    method: p ? 'PUT' : 'POST',
    submitLabel: p ? 'Save' : 'Add profile',
    onDone: opts.onDone,
  });
}
