/* The account settings modal (DESIGN.md §17.1 "Editing: a modal"), opened from the account
   page's Details card and its overflow. Part 2 of the Accounts conversion opens the same
   modal from the list page's row actions - it takes an account id and nothing else, so
   there is nothing page-specific to add there.

   `+ Add Account` deliberately stays a PAGE. It is the one surface that has to carry the
   type switch and the credentials before anything exists to attach them to, and a modal
   whose whole field set swaps under you is a worse first experience than a form.

   Three things about it are load-bearing:

   1. **The password is never served back out.** The field renders blank with "leave blank
      to keep existing", and its eyeball swaps the input's `type` so you can read WHAT YOU
      ARE TYPING RIGHT NOW - it is not §3.16's config-secret reveal, which fetches a stored
      value. There is no endpoint that returns an account password, `_is_sensitive_path()`
      is untouched, and blanking the field keeps the stored password rather than clearing
      it (app/routes/accounts.py::_type_fields).
   2. **The client-side checks are presentation, not enforcement.** app/routes/accounts.py
      runs the same rules through the same `_validate_account_form` the form page uses, and
      that is what actually protects the row (CLAUDE.md: enforcement lives server-side).
      The duplicate-account warning exists only there, because it is a question about the
      database.
   3. **The type switch shows one field set at a time**, the way the form page does, and
      switching it does not discard what was typed in the other - a mis-click should not
      cost you a pasted URL.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow).
*/

const ACCOUNT_DEFAULT_COLOR = '#58a6ff';

/* The eye pair from the secret_reveal_btn macro (templates/_macros.html). Duplicated as
   markup rather than shared, because that macro is Jinja and this control is built in JS -
   the CSS, which is the part worth sharing, is the shipped .secret-reveal-btn. */
const ACCOUNT_EYE_SVG =
  '<svg class="eye-on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>' +
  '<svg class="eye-off" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 ' +
  '4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/>' +
  '<line x1="1" y1="1" x2="23" y2="23"/></svg>';

/* First failing rule's message, or null. Mirrors _validate_account_form in
   app/routes/accounts.py, which is what actually rejects a bad save. */
function accountValidate(values) {
  if (!String(values.name || '').trim()) return 'Name is required.';
  if (values.account_type === 'm3u') {
    if (!String(values.m3u_url || '').trim()) return 'M3U URL is required.';
  } else {
    if (!String(values.base_url || '').trim()) return 'Base URL is required.';
    if (!String(values.username || '').trim()) return 'Username is required.';
  }
  const conn = String(values.max_connections || '').trim();
  if (conn && !(/^\d+$/.test(conn) && Number(conn) >= 1)) {
    return 'Max connections must be a positive whole number.';
  }
  return null;
}

/* The request body. An empty string means "no per-account override" for the two inherited
   settings and "keep the stored one" for the password - never "set it to blank". */
function accountPayload(values) {
  return {
    name: String(values.name || '').trim(),
    account_type: values.account_type,
    m3u_url: values.m3u_url,
    epg_url: values.epg_url,
    base_url: values.base_url,
    username: values.username,
    password: values.password,
    color: values.color || ACCOUNT_DEFAULT_COLOR,
    url_normalization: values.url_normalization,
    sync_interval_hours: values.sync_interval_hours,
    max_connections: values.max_connections,
    sync_enabled: !!values.sync_enabled,
    xtream_debug_override: !!values.xtream_debug_override,
  };
}

/* opts: { accountId, onDone } */
function openAccountModal(opts) {
  const accountId = opts.accountId;
  const onDone = opts.onDone || (() => window.location.reload());

  jsonFetch(`/api/accounts/${accountId}`)
    .then((data) => renderAccountModal(data, onDone))
    .catch((err) => showToast(err.message || 'Could not load this account.', { type: 'error' }));
}

function renderAccountModal(data, onDone) {
  const a = data.account;
  const normOptions = data.norm_options || [];
  const hours = data.sync_hours_choices || [];

  const normSelect =
    `<select id="acct-norm">${normOptions.map((o) =>
      `<option value="${escHtml(o.value)}" data-example="${escHtml(o.example || '')}"` +
      `${o.value === a.url_normalization ? ' selected' : ''}>${escHtml(o.label)}</option>`).join('')}</select>`;

  const hoursSelect =
    '<select id="acct-hours">' +
    `<option value=""${a.sync_interval_hours === '' ? ' selected' : ''}>` +
    `Default (${escHtml(String(data.global_sync_hours))}h - follows Settings)</option>` +
    hours.map((h) =>
      `<option value="${h}"${String(a.sync_interval_hours) === String(h) ? ' selected' : ''}>` +
      `Every ${h} hour${h === 1 ? '' : 's'}</option>`).join('') +
    '</select>';

  const body = document.createElement('div');
  body.innerHTML =
    fieldRow({
      label: 'Account type',
      meta: 'M3U works with any IPTV provider. Xtream API uses the provider\'s Xtream Codes login.',
      control: '<select id="acct-type">' +
        `<option value="m3u"${a.account_type === 'm3u' ? ' selected' : ''}>M3U playlist + XMLTV EPG</option>` +
        `<option value="xtream"${a.account_type === 'xtream' ? ' selected' : ''}>Xtream API</option>` +
        '</select>',
    }) +
    fieldRow({
      label: 'Name',
      meta: 'A friendly label shown wherever this account appears.',
      control: `<input type="text" id="acct-name" value="${escHtml(a.name)}">`,
      wide: true,
    }) +
    // ── M3U fields ──
    fieldRow({
      label: 'M3U URL', stack: true, id: 'm3u',
      meta: 'The full playlist URL from your provider. This is where the account DATA is ' +
        'fetched from - it is not where the streams come from, and the two are frequently ' +
        'different hosts.',
      control: `<input type="url" id="acct-m3u" value="${escHtml(a.m3u_url)}">`,
    }) +
    fieldRow({
      label: 'XMLTV EPG URL', stack: true, id: 'epg',
      meta: 'Guide data URL. Leave blank to skip EPG import.',
      control: `<input type="url" id="acct-epg" value="${escHtml(a.epg_url)}">`,
    }) +
    // ── Xtream fields ──
    fieldRow({
      label: 'Base URL', stack: true, id: 'base',
      meta: 'Provider URL without a trailing slash, e.g. <code>http://provider.com:8080</code>. ' +
        'This is the API endpoint, not the stream origin.',
      control: `<input type="text" id="acct-base" value="${escHtml(a.base_url)}">`,
    }) +
    fieldRow({
      label: 'Username', id: 'user',
      meta: 'The login ChannelBin authenticates with.',
      control: `<input type="text" id="acct-user" autocomplete="off" value="${escHtml(a.username)}">`,
      wide: true,
    }) +
    fieldRow({
      label: 'Xtream debug mode', id: 'debug',
      meta: (data.global_xtream_debug
        ? 'Debug mode is already on globally (Settings), so the Fetch &amp; Dump / Sync ' +
          'from dump tools are already available for every account.'
        : 'Enables the Fetch &amp; Dump / Sync from dump troubleshooting tools for THIS ' +
          'account only, without turning on debug mode for every account.') +
        ' A dump stores this account\'s username/password in plaintext, since every ' +
        'stream URL in it embeds them.',
      control: '<label class="switch"><input type="checkbox" id="acct-debug"' +
        `${a.xtream_debug_override ? ' checked' : ''}${data.global_xtream_debug ? ' disabled' : ''}>` +
        '<span class="knob"></span></label>',
    }) +
    fieldRow({
      label: 'Password', id: 'pass',
      meta: a.has_password
        ? 'Leave blank to keep the existing password. ChannelBin never shows a stored ' +
          'password back to you - the eye only reveals what you type here.'
        : 'No password is stored for this account yet.',
      // The shipped .secret-field / .secret-reveal-btn look, WITHOUT data-secret-path -
      // that attribute is what makes util.js::initSecretReveal fetch a stored value, and
      // no endpoint returns an account password.
      control: '<span class="secret-field">' +
        '<input type="password" id="acct-pass" autocomplete="new-password" ' +
        `placeholder="${a.has_password ? 'Leave blank to keep existing' : 'Password'}">` +
        '<button type="button" class="secret-reveal-btn" ' +
        'aria-label="Show what I am typing" title="Show what I am typing">' + ACCOUNT_EYE_SVG +
        '</button></span>',
      wide: true,
    }) +
    // ── Shared ──
    fieldRow({
      label: 'URL normalization', stack: true,
      meta: 'Rewrites every stream URL into one consistent form. Only the shape changes - ' +
        'the address and credentials the provider supplied are kept as-is, and a URL with ' +
        'no user/password/id in it is left untouched.' +
        '<div id="acct-norm-example" style="margin-top:.35rem;display:none">Example: ' +
        '<code id="acct-norm-example-url"></code></div>',
      control: normSelect +
        '<div style="margin-top:8px"><button type="button" class="btn btn-sm" ' +
        'id="acct-renorm-btn">Re-normalize existing channels</button></div>',
    }) +
    fieldRow({
      label: 'Sync interval',
      meta: 'How often this account\'s channels and guide data are refreshed.',
      control: hoursSelect,
    }) +
    fieldRow({
      label: 'Max simultaneous connections',
      meta: 'The cap on concurrent connections to this provider - recordings and channel ' +
        'tests combined. Account sync does not count against it. Blank follows the global ' +
        `default (${escHtml(String(data.global_max_connections))}).`,
      control: '<input type="number" id="acct-conn" min="1" step="1" ' +
        `value="${escHtml(String(a.max_connections))}" placeholder="Default (${escHtml(String(data.global_max_connections))})">`,
    }) +
    fieldRow({
      label: 'Automatic sync',
      meta: 'When off, this account is never synced on a schedule. Sync now still works.',
      control: '<label class="switch"><input type="checkbox" id="acct-auto"' +
        `${a.sync_enabled ? ' checked' : ''}><span class="knob"></span></label>`,
    }) +
    fieldRow({
      label: 'Color', stack: true,
      meta: 'Identifies this account\'s channels in the TV Guide and the channel browser.',
      control: colorPickerHtml('acct-', a.color, ACCOUNT_DEFAULT_COLOR, data.preset_colors),
    });

  const $ = (sel) => body.querySelector(sel);

  // One field set on screen at a time, the same rule the form page follows. Nothing is
  // cleared when the type changes - a mis-click must not cost a pasted URL.
  const M3U_ROWS = ['m3u', 'epg'];
  const XTREAM_ROWS = ['base', 'user', 'pass', 'debug'];
  const applyType = () => {
    const type = $('#acct-type').value;
    M3U_ROWS.forEach((id) => {
      body.querySelector(`[data-frow="${id}"]`).style.display = type === 'm3u' ? '' : 'none';
    });
    XTREAM_ROWS.forEach((id) => {
      body.querySelector(`[data-frow="${id}"]`).style.display = type === 'xtream' ? '' : 'none';
    });
  };
  $('#acct-type').addEventListener('change', applyType);
  applyType();

  // The example under the normalization dropdown. An upgrade of an already-correct state:
  // it starts hidden and only ever adds information.
  const exampleBox = $('#acct-norm-example');
  const exampleUrl = $('#acct-norm-example-url');
  const showExample = () => {
    const example = $('#acct-norm').selectedOptions[0]?.dataset.example || '';
    exampleUrl.textContent = example;
    exampleBox.style.display = example ? '' : 'none';
  };
  $('#acct-norm').addEventListener('change', showExample);
  showExample();

  // Reveals what is being typed, nothing else - a local `type` swap with no request behind
  // it (DESIGN.md §17.4). Wired by util.js, shared with the Add-account form, because this
  // modal and that page render the identical control and had identical copies of it.
  initLocalReveal(body);

  const readColor = wireColorPicker(body, 'acct-', ACCOUNT_DEFAULT_COLOR);

  const read = () => ({
    name: $('#acct-name').value,
    account_type: $('#acct-type').value,
    m3u_url: $('#acct-m3u').value,
    epg_url: $('#acct-epg').value,
    base_url: $('#acct-base').value,
    username: $('#acct-user').value,
    password: $('#acct-pass').value,
    color: readColor(),
    url_normalization: $('#acct-norm').value,
    sync_interval_hours: $('#acct-hours').value,
    max_connections: $('#acct-conn').value,
    sync_enabled: $('#acct-auto').checked,
    xtream_debug_override: $('#acct-debug').checked,
  });

  function submit(close) {
    const values = read();
    const error = accountValidate(values);
    if (error) { showToast(error, { type: 'error' }); return; }
    jsonFetch(`/api/accounts/${a.id}`, {
      method: 'POST',
      body: JSON.stringify(accountPayload(values)),
    }).then((res) => {
      // The duplicate warning is advisory and never blocks the save, so it is shown
      // alongside the success rather than instead of it.
      if (res.warning) showToast(res.warning, { type: 'error', durationMs: 9000 });
      showToast(res.message || 'Account saved.');
      close();
      onDone();
    }).catch((err) => showToast(err.message, { type: 'error' }));
  }

  // Paired with the URL normalization field (2026-08-04): the only reason
  // to re-normalize is that the mode just changed, so this always saves the current form
  // (including the mode dropdown) first, then rewrites every existing channel's stream_url
  // to match - no provider connection, app/routes/accounts.py::renormalize_urls_api.
  function renormalize() {
    const values = read();
    const error = accountValidate(values);
    if (error) { showToast(error, { type: 'error' }); return; }
    const modeLabel = $('#acct-norm').selectedOptions[0]?.textContent || 'the selected mode';
    buildModal({
      title: 'Re-normalize existing channels?',
      body: `Save the current settings, then rewrite every existing channel's stream URL ` +
        `to the "${escHtml(modeLabel)}" form now. This does not contact the provider.`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        { label: 'Save & re-normalize', class: 'btn btn-primary', onClick: (close) => {
            close();
            jsonFetch(`/api/accounts/${a.id}`, {
              method: 'POST',
              body: JSON.stringify(accountPayload(values)),
            }).then((res) => {
              if (res.warning) showToast(res.warning, { type: 'error', durationMs: 9000 });
              return jsonFetch(`/api/accounts/${a.id}/renormalize-urls`, { method: 'POST' });
            }).then((res) => {
              showToast(res.message || 'Channel URLs re-normalized.');
              modalEl.closeModal();
              onDone();
            }).catch((err) => showToast(err.message, { type: 'error' }));
          } },
      ],
    });
  }
  $('#acct-renorm-btn').addEventListener('click', renormalize);

  const modalEl = buildModal({
    title: `Edit "${a.name}"`,
    panelClass: 'modal-wide',
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Save', class: 'btn btn-primary',
        onClick: (close) => { submit(close); return false; } },
    ],
  });
}
