// Unified group / health-check detail page (templates/channels/group_detail.html).
// Design: dev/changelog/272 (mockups 14 desktop + 15 mobile); rollout: dev/changelog/273.
//
// One page, two entry URLs, three states. `G.hasChannel` / `G.hasCheck` gate every
// facet-specific column, action and section, exactly as the mockups' facets() did.
//
// This file is the SOLE renderer of the Channels table: the server ships the first
// batch of rows in `G.rows` and the live-refresh endpoint returns the same shape, so
// there is no second (Jinja) copy of a row to drift out of step with this one.
(() => {
  'use strict';

  const G = window.GROUP_DETAIL;
  const api = (path) => `/api/channel-groups/${G.groupId}/${path}`;
  const jobApi = (path) => `/api/channel-tests/on-demand/${G.jobId}${path ? '/' + path : ''}`;

  let ROWS = G.rows.slice();
  let DUP_GROUPS = G.dupGroups.slice();
  let MISSING_CHANNELS = G.missingChannels.slice();
  let COUNTS = G.counts;
  let TOTAL = G.total;
  // Everything section 16's banners are gated and counted on, decided server-side in
  // routes/channel_groups.py::_banner_facts and re-read on every refresh. Null on the
  // system group, which has no memberships and therefore nothing to warn about.
  let WARN = G.warnings;

  const strategyValue = () => (WARN && WARN.strategy) || 'health_check_only';

  // The status options a row can be filtered by. Order = display order. TESTING is
  // deliberately absent: it is transient, and a row under test is filtered by the status
  // it is being re-measured FROM (see filterStatus()).
  // `DISABLED` only exists on the system group, whose membership is computed and whose
  // rows carry Channel.test_enabled instead of the two participation switches. On a
  // stored group nothing is "disabled" any more (DECIDED 3), so offering the option
  // would be a filter that can never match a row.
  const STATUS_FILTER = [
    ['PASS', 'Pass'], ['WARN', 'Warn'], ['FAIL', 'Fail'],
    ['WAITING', 'Untested'], ['CANCELLED', 'Cancelled'],
  ].concat(G.hasChannel ? [] : [['DISABLED', 'Disabled']]);

  let searchTerm = '';
  let sortKey = 'score';
  let sortDir = -1;               // score descending, name A-Z tie-break
  const expanded = new Set();
  const selected = new Set();
  // Phone only. A checkbox on every card costs a tap target's width on every row for
  // something most visits never use, so selection is a MODE the Select chip turns on
  // (mockup 34's M3). The desktop's checkbox column is always there and ignores this.
  let selecting = false;

  let testerStatus = {};
  let pollTimer = null;
  let wasRunning = false;
  let reloading = false;
  let lastLogSeq = 0;             // highest server log `seq` already rendered

  const LOG_COLORS = { INFO: 'var(--text-muted)', SUCCESS: 'var(--ok)', WARN: 'var(--warn)', ERROR: 'var(--bad)' };

  // ── Small helpers ─────────────────────────────────────────────────────────

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const byId = (id) => document.getElementById(id);

  function rowScore(r) {
    if (r.health_score === null || r.health_score === undefined) return null;
    return Math.max(0, Math.min(100, Math.round(r.health_score + (r.manual_health_adjustment || 0))));
  }

  // A row under test right now outranks its stored result: showing the previous
  // outcome while it is being re-measured is a lie about the current state.
  function rowStatus(r) {
    if (testerStatus.is_running && testerStatus.current_job_id === G.jobId
        && testerStatus.current_channel_id === r.channel_id) return 'TESTING';
    return filterStatus(r);
  }

  // What the Status filter matches on: the same thing minus the transient TESTING
  // overlay, so a filtered list does not blink a row out for the few seconds a test sits
  // on it - and so the per-value counts in the filter popover stay honest, which they
  // could not be if a row under test matched every status at once.
  function filterStatus(r) {
    if (r.disabled) return 'DISABLED';
    if (!r.last_test) return 'WAITING';
    return testStatusLabel(r.last_test);
  }

  // Anything the drawer can actually show opens it. `audio_codec` is in this list because
  // a test that probed audio but no video profile still has five audio facts to show, and
  // leaving it out sealed them behind a caret that never rendered - recorded, serialized to
  // this page, and unreachable (dev/changelog/769). Whatever is added to the drawer belongs
  // here in the same edit, or it ships invisible.
  function canExpand(r) {
    const t = r.last_test;
    return !!(t && (t.video_codec || t.audio_codec || t.screenshot_filename || t.error_detail));
  }

  function showActionError(msg) {
    const el = byId('gd-action-error');
    if (!el) return;
    el.textContent = msg;
    el.style.display = msg ? '' : 'none';
  }

  function postAndReload(url, method = 'POST', body = null) {
    showActionError('');
    return jsonFetch(url, body ? { method, body: JSON.stringify(body) } : { method })
      .then(() => { reloading = true; location.reload(); })
      .catch(e => showActionError(e.message || 'Request failed.'));
  }

  // Inline modal (not window.prompt()) so a rejected rename - duplicate name, empty,
  // too long - shows its error next to the still-typed name instead of losing the
  // input the instant the user clicks OK. Matches the "New group" create modal's
  // safety net (static/js/groups.js): the button never auto-closes on the async
  // request path, only on success.
  function openRenameModal() {
    const body = document.createElement('div');
    body.innerHTML = `
      <div id="gd-rename-error" style="display:none; color:var(--bad); margin-bottom:0.75rem; font-size:0.875rem;"></div>
      <div class="form-group">
        <label style="font-size:12px; margin-bottom:2px; display:block;">Group name</label>
        <input type="text" id="gd-rename-input" class="form-control" value="${escHtml(G.groupName)}" maxlength="255">
      </div>`;
    const errEl = body.querySelector('#gd-rename-error');
    const input = body.querySelector('#gd-rename-input');
    const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = ''; };

    const submit = (close) => {
      const name = input.value.trim();
      if (!name) { showErr('Group name is required.'); return false; }
      if (name.length > 255) { showErr('Group name must be 255 characters or fewer.'); return false; }
      if (name === G.groupName) { close(); return false; }
      errEl.style.display = 'none';
      jsonFetch(api('rename'), { method: 'POST', body: JSON.stringify({ name }) })
        .then(() => { reloading = true; location.reload(); })
        .catch(e => showErr(e.message || 'Rename failed.'));
      return false;
    };

    const modal = buildModal({
      title: 'Rename group',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (close) => close() },
        { label: 'Rename', class: 'btn btn-primary', onClick: submit },
      ],
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); submit(modal.closeModal); }
    });
    input.focus();
    input.select();
  }

  // ── Section layout (order + hidden), persisted server-side ────────────────
  // Stored separately per page state: a health-check-only page has no Group format
  // section to order, so a shared layout would advertise sections that cannot appear.

  const SEC_NAMES = { summary: 'Summary', linked: G.linkedTitle || 'Linked',
                      settings: 'Settings', channels: 'Channels', activity: 'Activity timeline' };
  const secStateName = { both: 'channel group + health check', channel: 'channel group only', check: 'health check only' }[G.secKey];
  const sectionLayout = initSectionLayout({
    config: G,
    saveUrl: `/api/user-prefs/group_detail_sections_${G.secKey}`,
    names: SEC_NAMES,
    note: `These picks apply to <strong>${escHtml(secStateName)}</strong> pages only - the other ` +
      'states keep their own layout. The title, status bar and warning banners always show.',
  });

  // ── Column layout, persisted server-side (DESIGN.md 3.11) ─────────────────

  const COL_LABEL = { rec: 'Recording', test: 'Health check', status: 'Status', score: 'Score', res: 'Format', fps: 'FPS', audio: 'Audio', framePct: 'Frames', bitrate: 'Bitrate', drops: 'Drops', shot: 'Screenshot', epg: 'EPG id', account: 'Account' };
  const COL_SORTABLE = { rec: true, test: true, status: true, score: true, res: true, fps: true, audio: true, framePct: true, bitrate: true, drops: true, shot: false, epg: true, account: false };
  // Entries that are hideable FIELDS rather than columns: they render inside the Channel
  // cell (the account dot) or on the phone card's own line, so they have no <td> of their
  // own and no column position to drag. They are in `colState` regardless, because "show
  // the account at all" is a real question for the many users with one account, and one
  // toggle serving both widths beats a second field-visibility store (dev/changelog/758).
  const FIELD_ONLY = { account: true };
  // The two participation headers carry the tooltip that says what the switch under them
  // means - the labels are one word each and neither is self-explanatory.
  const COL_TIP = {
    rec: 'Recording.&#10;Eligible to be picked for a scheduled recording. ChannelBin chooses among ' +
      'the enabled members by health score. Nothing automatic moves this switch in either ' +
      'direction - it is yours.',
    test: "Health check.&#10;Included when this group's health check runs.",
    epg: 'EPG id.&#10;The XMLTV channel id this member&#39;s listings are matched on. A group fills ' +
      'one guide row per program from whichever member wins, so members carrying different ids ' +
      'paint that row from unrelated schedules.',
  };
  const PART_COLS = { rec: 'recording_enabled', test: 'test_enabled' };

  let colState = (G.columnPref && Array.isArray(G.columnPref.order))
    ? { order: G.columnPref.order.slice(), hidden: (G.columnPref.hidden || []).slice() }
    : { order: G.columns.slice(), hidden: G.columns.filter(k => G.columnsOff.includes(k)) };
  // A column added after this browser stored its layout is NEW to that layout, so it takes
  // the app's own default rather than "on": a stored `hidden` list cannot say anything about
  // a key that did not exist when it was written, and reading its silence as "show it" ships
  // every future default-off column switched on for exactly the users who have used the page
  // before (dev/docs/BUGS.md 2026-09-09, dev/changelog/898).
  G.columns.forEach(k => {
    if (colState.order.includes(k)) return;
    colState.order.push(k);
    if (G.columnsOff.includes(k) && !colState.hidden.includes(k)) colState.hidden.push(k);
  });
  colState.order = colState.order.filter(k => G.columns.includes(k));

  // The one reader of the shared visibility set, asked by the table, by the card and by
  // both field pickers, so a field cannot be on in one drawing and off in the other.
  const fieldOn = (k) => !colState.hidden.includes(k);

  function toggleField(k, on) {
    if (on) colState.hidden = colState.hidden.filter(x => x !== k);
    else if (!colState.hidden.includes(k)) colState.hidden.push(k);
    saveColumns();
  }

  function saveColumns() {
    jsonFetch(`/api/user-prefs/group_detail_columns_${G.colKey}`, {
      method: 'POST', body: JSON.stringify({ value: colState }),
    }).catch(() => showToast('Could not save the column setup.', { type: 'error' }));
  }

  // Structural columns are never in the picker - Channel is required the same way the
  // section picker never lists the page header.
  function columns() {
    const cols = [];
    // Selection exists for the bulk verbs, and a stored group has two of them (the
    // participation switches) whether or not it carries a health check to test with.
    if (G.hasCheck || G.hasChannel) cols.push({ k: 'select', label: '', sort: false });
    cols.push({ k: 'caret', label: '', sort: false });
    cols.push({ k: 'name', label: 'Channel', sort: true });
    colState.order.filter(k => !FIELD_ONLY[k] && fieldOn(k))
      .forEach(k => cols.push({ k, label: COL_LABEL[k], sort: COL_SORTABLE[k], tip: COL_TIP[k] }));
    cols.push({ k: 'acts', label: '', sort: false });
    return cols;
  }

  function buildColMenu() {
    const menu = byId('gd-col-menu');
    menu.querySelectorAll('.col-item').forEach(el => el.remove());
    const note = menu.querySelector('.pop-note');
    colState.order.forEach(key => {
      const item = document.createElement('label');
      item.className = 'col-item';
      // A field-only entry gets no grip and is not draggable: it renders inside another
      // cell, so there is no column position for a drag to move and a grip that did
      // nothing would be a control that lies.
      item.draggable = !FIELD_ONLY[key];
      item.dataset.key = key;
      item.innerHTML = (FIELD_ONLY[key] ? '<span class="grip"></span>' : `<span class="grip">&#8942;&#8942;</span>`) +
        `<input type="checkbox" ${fieldOn(key) ? 'checked' : ''}> ${escHtml(COL_LABEL[key])}`;
      item.querySelector('input').addEventListener('change', (e) => {
        toggleField(key, e.target.checked);
        renderList();
      });
      if (!FIELD_ONLY[key]) {
        item.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', key));
        item.addEventListener('dragover', (e) => e.preventDefault());
        item.addEventListener('drop', (e) => {
          e.preventDefault();
          const from = e.dataTransfer.getData('text/plain');
          if (!from || from === key) return;
          const order = colState.order.filter(k => k !== from);
          order.splice(order.indexOf(key), 0, from);
          colState.order = order;
          saveColumns();
          buildColMenu();
          renderList();
        });
      }
      menu.insertBefore(item, note);
    });
  }

  // ── Filters (DESIGN.md 3.11) ──────────────────────────────────────────────
  // One registry, three consumers: the + Filter popover and its chips (drawn by the
  // shared static/js/filter-bar.js), the phone's Filters sheet, and matches() itself.
  // Another dimension is an entry here and nothing else - never a second control on the
  // bar, and never a second predicate, which is how the guide's health and tag filters
  // each came to be missed by one of their two consumers in turn. Format, FPS and Tag
  // were added exactly that way and cost no edit outside this array (dev/changelog/768).
  //
  // Recording and Health check are two dimensions rather than one "Participation" so
  // they keep ANDing: "recording on AND not tested" is the setup 16.1 warns about, so it
  // has to be a question the list can answer directly.

  const accountValues = () => {
    const seen = new Map();
    ROWS.forEach(r => {
      if (r.account_id != null && !seen.has(String(r.account_id))) {
        seen.set(String(r.account_id), r.account_name || `Account ${r.account_id}`);
      }
    });
    return Array.from(seen.entries())
      .sort((a, b) => a[1].toLowerCase().localeCompare(b[1].toLowerCase()))
      .map(([v, label]) => ({ v, label }));
  };

  // The (resolution, rounded-fps) bucket one member's own last test measured, spelled the
  // way app/channel_groups.py::format_label spells it so a chip, the Format column and the
  // group's reference label all read as the same string. The rounding is not cosmetic and
  // is never skipped: on the raw float 59.94 and 60 are two buckets, so a group locked to
  // "1920x1080 @ 60" would have a filter value matching none of its own members.
  const rowFormatKey = (r) => {
    const t = r.last_test;
    if (!t || !t.resolution || !t.fps) return null;
    return `${t.resolution} @ ${Math.round(t.fps)}`;
  };
  const rowFps = (r) => (r.last_test && r.last_test.fps ? Math.round(r.last_test.fps) : null);
  // A member whose format is UNKNOWN is its own bucket rather than a row no value can
  // reach: on a page whose banners are about which formats these members span, "which of
  // these has never been measured" is one of the questions being asked. 'none' can never
  // collide with a real key - every one of those is "WxH @ N".
  const FMT_NONE = 'none';

  const formatValues = () => {
    const seen = new Set();
    let unmeasured = false;
    ROWS.forEach(r => {
      const k = rowFormatKey(r);
      if (k) seen.add(k); else unmeasured = true;
    });
    const height = (k) => parseInt(k.split('x')[1], 10) || 0;
    const fps = (k) => parseInt(k.split('@')[1], 10) || 0;
    const out = Array.from(seen)
      .sort((a, b) => (height(b) - height(a)) || (fps(b) - fps(a)) || a.localeCompare(b))
      .map(k => ({ v: k, label: k }));
    if (unmeasured) out.push({ v: FMT_NONE, label: 'Not measured' });
    return out;
  };

  const fpsValues = () => {
    const seen = new Set();
    ROWS.forEach(r => { const n = rowFps(r); if (n) seen.add(n); });
    return Array.from(seen).sort((a, b) => b - a).map(n => ({ v: String(n), label: `${n} fps` }));
  };

  // Tag names are user text, so the key is lowercased at BOTH ends - build here, lookup in
  // match() - rather than compared as typed with a fallback, which permanently shadows every
  // case-variant (CLAUDE.md, keyed lookups). The label keeps the tag's own spelling.
  const tagValues = () => {
    const seen = new Map();
    ROWS.forEach(r => (r.tags || []).forEach(t => {
      if (t && t.name && !seen.has(t.name.toLowerCase())) seen.set(t.name.toLowerCase(), t.name);
    }));
    return Array.from(seen.entries())
      .sort((a, b) => a[0].localeCompare(b[0]))
      .map(([v, label]) => ({ v, label }));
  };

  // The three audio dimensions read the SEPARATE fields, never `audio_summary`. That string
  // is codec, channel count and sample rate glued together for display, so filtering on it
  // produces one bucket per unique combination and answers no question anybody has.
  //
  // ffprobe's codec and language strings vary in case and spelling across providers, so the
  // key is lowercased at BOTH ends - build here, lookup in match() - never compared as typed
  // with a fallback, which permanently shadows every case-variant (CLAUDE.md, keyed lookups).
  // The label keeps a codec's conventional upper-case spelling.
  const audioCodecValues = () => {
    const seen = new Set();
    ROWS.forEach(r => { const c = r.last_test && r.last_test.audio_codec; if (c) seen.add(c.toLowerCase()); });
    return Array.from(seen).sort().map(v => ({ v, label: v.toUpperCase() }));
  };

  const audioLangValues = () => {
    const seen = new Set();
    ROWS.forEach(r => { const l = r.last_test && r.last_test.audio_language; if (l) seen.add(l.toLowerCase()); });
    return Array.from(seen).sort().map(v => ({ v, label: v }));
  };

  // Mono/Stereo/5.1 rather than the raw integer: nobody picks a feed by "6", and the layout
  // names are what a provider's own listing uses. An unnamed count still gets a row rather
  // than being dropped - "7ch" is rare, not impossible, and a value that exists must be
  // reachable.
  const CH_LAYOUT = { 1: 'Mono', 2: 'Stereo', 6: '5.1', 8: '7.1' };
  const audioChValues = () => {
    const seen = new Set();
    ROWS.forEach(r => { const n = r.last_test && r.last_test.audio_channels; if (n) seen.add(n); });
    return Array.from(seen).sort((a, b) => a - b)
      .map(n => ({ v: String(n), label: CH_LAYOUT[n] || `${n}ch` }));
  };

  // A dimension is worth a menu row when choosing one of its values would EXCLUDE at least
  // one member. "More than one value" is not that test for anything a health check measures:
  // a member that was never tested carries no value at all, so on a group whose 17 measured
  // members are all AAC and whose other 7 have never run, picking AAC still narrows 24 rows
  // to 17. Format escapes this only because it carries an explicit "Not measured" bucket that
  // counts toward its own length - the test-derived dimensions below have none by design
  // (an untested member matching no audio value is correct), so they ask this instead.
  // Account is not routed through it: every row has one whether or not it was ever tested,
  // so nothing is missing and > 1 already says the same thing (dev/changelog/770).
  // The EPG dimension's values. Unlike a format key, an EPG id is provider-supplied text
  // that could be anything - `none` included - so a bare sentinel for the missing bucket
  // would be a key collision waiting on one badly-named channel. Real ids therefore carry
  // an `id:` prefix and the sentinel stands alone, which also survives the round trip
  // through the `data-fval` attribute the filter bar reads values back from.
  const EPG_NONE = 'none';
  const epgKey = (id) => `id:${id}`;
  const epgValues = () => {
    const seen = new Map();
    let missing = false;
    ROWS.forEach(r => {
      if (r.epg_channel_id) seen.set(r.epg_channel_id, true);
      else missing = true;
    });
    const out = Array.from(seen.keys())
      .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
      .map(id => ({ v: epgKey(id), label: id }));
    // Its own bucket rather than nothing, for the same reason Format carries "Not measured":
    // a member with no id would otherwise be a row no value can select, on the one page
    // whose banner is about which ids these members carry. And it is not a mismatch -
    // unknown is not proven-different (DESIGN-channel-groups-model.md 8).
    if (missing) out.push({ v: EPG_NONE, label: 'No EPG id' });
    return out;
  };

  const partitions = (values, has) => {
    const n = values().length;
    return n > 1 || (n === 1 && ROWS.some(r => !has(r)));
  };

  const FILTER_DIMS = [
    { k: 'rec',
      label: 'Recording',
      values: [{ v: 'on', label: 'Eligible' }, { v: 'off', label: 'Not eligible' }],
      match: (r, v) => !!r.recording_enabled === (v === 'on'),
      available: () => G.hasChannel },
    { k: 'test',
      label: 'Health check',
      values: [{ v: 'on', label: 'Tested by this group' }, { v: 'off', label: 'Not tested' }],
      match: (r, v) => !!r.test_enabled === (v === 'on'),
      available: () => G.hasChannel },
    { k: 'status',
      label: 'Status',
      values: STATUS_FILTER.map(([v, label]) => ({ v, label })),
      match: (r, v) => filterStatus(r) === v,
      available: () => G.hasCheck },
    // Format and FPS are two dimensions for the same reason Recording and Health check
    // are: one combined bucket cannot answer "every 60fps member, whatever its
    // resolution", which is a real question on a group that spans three formats.
    { k: 'fmt',
      label: 'Format',
      values: formatValues,
      match: (r, v) => (rowFormatKey(r) || FMT_NONE) === v,
      // A single bucket means every member already shares it, so the filter could only
      // ever select all of them - the same "not a question worth a menu row" gate the
      // account dimension uses. Tag is not gated that way below because tags do not
      // partition the list: one tag still splits it into carriers and non-carriers.
      available: () => formatValues().length > 1 },
    { k: 'fps',
      label: 'FPS',
      values: fpsValues,
      match: (r, v) => { const n = rowFps(r); return n !== null && String(n) === v; },
      available: () => partitions(fpsValues, r => rowFps(r) !== null) },
    // A member with no test carries no audio and therefore matches no audio value. That is
    // right and needs no "Not measured" bucket the way Format has one: this is a filter the
    // user drives, not the format lock, so the "an untested member is never filtered out"
    // rule does not reach it (DESIGN-channel-groups-model.md 5).
    { k: 'audio',
      label: 'Audio codec',
      values: audioCodecValues,
      match: (r, v) => {
        const c = r.last_test && r.last_test.audio_codec;
        return !!c && c.toLowerCase() === v;
      },
      available: () => partitions(audioCodecValues, r => !!(r.last_test && r.last_test.audio_codec)) },
    { k: 'alang',
      label: 'Language',
      values: audioLangValues,
      match: (r, v) => {
        const l = r.last_test && r.last_test.audio_language;
        return !!l && l.toLowerCase() === v;
      },
      available: () => partitions(audioLangValues, r => !!(r.last_test && r.last_test.audio_language)) },
    { k: 'ach',
      label: 'Audio channels',
      values: audioChValues,
      match: (r, v) => {
        const n = r.last_test && r.last_test.audio_channels;
        return !!n && String(n) === v;
      },
      available: () => partitions(audioChValues, r => !!(r.last_test && r.last_test.audio_channels)) },
    { k: 'account',
      label: 'Account',
      values: accountValues,
      match: (r, v) => String(r.account_id) === v,
      // One account across every member is not a question worth a menu row.
      available: () => accountValues().length > 1 },
    // The dimension the EPG mismatch banner points at. Gated the same way Format is - one
    // bucket means every member already shares an id (or the lack of one), so the filter
    // could only ever select all of them, and the banner that would send you here does not
    // fire in that state either.
    { k: 'epg',
      label: 'EPG id',
      values: epgValues,
      match: (r, v) => (v === EPG_NONE ? !r.epg_channel_id : epgKey(r.epg_channel_id) === v),
      available: () => epgValues().length > 1 },
    { k: 'tag',
      label: 'Tag',
      values: tagValues,
      match: (r, v) => (r.tags || []).some(t => t && t.name && t.name.toLowerCase() === v),
      available: () => tagValues().length > 0 },
    { k: 'age',
      label: 'Test age',
      values: [{ v: 'stale24', label: 'Not tested in 24h' }, { v: 'never', label: 'Never tested' }],
      // Never-tested is stale by any reading, so it matches both - a member with no test
      // at all is exactly what "not tested in 24h" is being asked about.
      match: (r, v) => {
        const at = r.last_test && r.last_test.tested_at;
        if (v === 'never') return !at;
        return !at || (Date.now() - new Date(at + 'Z').getTime()) > 86400000;
      },
      available: () => G.hasCheck },
  ];

  const filterBar = createFilterBar({
    chipsEl: byId('gd-filter-chips'),
    menuEl: byId('gd-filter-menu'),
    dims: FILTER_DIMS,
    rows: () => ROWS,
    note: 'Choices inside one filter are an "or"; different filters are an "and".',
    onChange: () => afterFilterChange(true),
  });

  // ── Table ─────────────────────────────────────────────────────────────────

  function sortValue(r, k) {
    switch (k) {
      case 'name': return r.channel_name.toLowerCase();
      case 'rec': return r.recording_enabled ? 1 : 0;
      case 'test': return r.test_enabled ? 1 : 0;
      case 'status': return ({ FAIL: 6, WARN: 5, PASS: 4, TESTING: 3, WAITING: 2, CANCELLED: 1, DISABLED: 0 })[rowStatus(r)] || 0;
      case 'score': { const s = rowScore(r); return s === null ? -1 : s; }
      case 'res': return r.last_test && r.last_test.resolution
        ? (parseInt(r.last_test.resolution.split('x')[1], 10) || -1) : -1;
      case 'fps': return r.last_test && r.last_test.fps ? r.last_test.fps : -1;
      // Codec first, then channel count, so the stereo and 5.1 feeds of one codec land
      // together instead of interleaving with another codec's. The empty string is the
      // untested sentinel: a column's first click sorts descending, which puts it at the
      // bottom exactly as the numeric columns' -1 does.
      case 'audio': {
        const t2 = r.last_test;
        if (!t2 || !t2.audio_codec) return '';
        return `${t2.audio_codec.toLowerCase()} ${String(t2.audio_channels || 0).padStart(2, '0')}`;
      }
      case 'framePct': return r.last_test && r.last_test.frame_pct !== null && r.last_test.frame_pct !== undefined
        ? r.last_test.frame_pct : -1;
      case 'bitrate': return r.last_test && r.last_test.bitrate_kbps ? r.last_test.bitrate_kbps : -1;
      case 'drops': return r.last_test && r.last_test.drop_count !== null && r.last_test.drop_count !== undefined
        ? r.last_test.drop_count : -1;
      // Lowercased so case-variant ids sort together - the same normalization
      // `sync.epg_case_sensitive_matching` off makes when it decides two ids are one
      // channel. The empty string is the no-id sentinel, matching the untested columns'
      // -1: a first click sorts descending and puts it at the bottom.
      case 'epg': return (r.epg_channel_id || '').toLowerCase();
      default: return -1;
    }
  }

  // Every filter dimension reaches this one predicate, through the one registry above.
  // Adding another means an entry in FILTER_DIMS, not a second consumer here that can
  // shadow rows the first one showed.
  function matches(r) {
    const term = searchTerm.trim();
    if (term && !globMatch(r.channel_name, term)) return false;
    return filterBar.matches(r);
  }

  function visibleRows() {
    const rows = ROWS.filter(matches);
    rows.sort((a, b) => {
      const av = sortValue(a, sortKey), bv = sortValue(b, sortKey);
      let r = typeof av === 'string' ? av.localeCompare(bv) : av - bv;
      r *= sortDir;
      if (r === 0) r = a.channel_name.toLowerCase().localeCompare(b.channel_name.toLowerCase());
      return r;
    });
    return rows;
  }

  function statusBadge(r) {
    const st = rowStatus(r);
    const t = r.last_test;
    if (st === 'DISABLED') {
      return `<span class="badge b-abort tip-plain" data-tip="Disabled.&#10;${escHtml(r.disabled_tip || 'Excluded here. Still usable everywhere else.')}">&#10005; Disabled</span>` +
        (t ? `<div class="gd-sub">Last: ${testStatusLabel(t)}</div>` : '');
    }
    // b-running: a channel simply being tested is not an alarm (dev/changelog/816).
    if (st === 'TESTING') return '<span class="badge b-running"><span class="pulse"></span>Testing</span>';
    if (st === 'WAITING') return '<span class="badge b-abort tip-plain" data-tip="Untested.&#10;No health check has reached this channel yet, so nothing is known about it. It is not assumed good.">Untested</span>';
    if (st === 'PASS') return '<span class="badge b-done tip-plain" data-tip="Pass.&#10;The last health check connected and it played cleanly for the whole test.">Pass</span>' +
      (t && t.connect_attempts > 1 ? `<div class="gd-sub">${t.connect_attempts} tries</div>` : '');
    if (st === 'WARN') return `<span class="badge b-warn tip-plain" data-tip="Warn.&#10;${escHtml((t && t.error_detail) || 'It played, but not cleanly - a slow connect, a stall, or a low bitrate. The first thing failover should skip.')}">Warn</span>`;
    if (st === 'CANCELLED') return `<span class="badge b-abort tip-plain" data-tip="Cancelled.&#10;${escHtml((t && t.error_detail) || 'The test was aborted before it finished, so it says nothing about this feed.')}">Cancelled</span>`;
    return `<span class="badge b-fail tip-plain" data-tip="Fail.&#10;${escHtml((t && t.error_detail) || 'The last health check could not play this feed at all.')}">Fail</span>`;
  }

  function scoreCell(r) {
    const s = rowScore(r);
    if (s === null) return '<span class="text-muted tip-plain" data-tip="No score.&#10;A score needs at least one completed test or recording on this feed.">&mdash;</span>';
    const n = r.health_score_sample_count || 0;
    const cls = healthBandCss(s);
    return `<span class="num hb-text ${cls} tip-plain" data-tip="Health score ${s}.&#10;Lifetime 0-100 from this feed's own history: connect time, stalls, and whether it held its declared format. Built from ${plural(n, 'observation')}.">&#9733;${s}</span>`;
  }

  function bitrateCell(t) {
    if (!t || t.bitrate_kbps === null || t.bitrate_kbps === undefined) return '<span class="text-muted">&mdash;</span>';
    const str = `${(t.bitrate_kbps / 1000).toFixed(2)} MB/s`;
    const w = parseInt(((t.resolution || '').split('x')[0]) || '0', 10);
    const threshold = w >= 1920 ? 1000 : (w >= 1280 ? 600 : 300);
    if (t.bitrate_kbps >= threshold) return str;
    return `<span class="val-warn tip-plain" data-tip="Low bitrate.&#10;Low for ${escHtml(t.resolution || 'this resolution')} - the picture may look soft or blocky in motion.">${str}</span>`;
  }

  function shotCell(t) {
    if (!t || !t.screenshot_filename) {
      return '<span class="gd-shot-none tip-plain" data-tip="No screenshot.&#10;None was captured for this test.">No shot</span>';
    }
    if (t.screenshot_pruned) {
      return '<span class="gd-shot-none tip-plain" data-tip="Pruned.&#10;The screenshot was deleted by the retention policy. Change how many are kept in Settings &rsaquo; Channel Health Checks.">Pruned</span>';
    }
    const src = `/channel-tests/screenshots/${encodeURIComponent(t.screenshot_filename)}`;
    return `<img src="${escHtml(src)}" class="gd-shot" loading="lazy" alt="Screenshot"
              data-shot="${escHtml(src)}" onerror="this.outerHTML='<span class=&quot;gd-shot-none&quot;>Missing</span>'">`;
  }

  function logoHtml(r) {
    if (r.logo_url) return `<img src="${escHtml(r.logo_url)}" class="gd-logo-img" alt="" onerror="this.style.opacity=0">`;
    const init = (r.channel_name.replace(/[^A-Za-z0-9 ]/g, '').split(/\s+/).filter(Boolean)
      .slice(0, 2).map(w => w[0]).join('') || '?').toUpperCase();
    return `<span class="gd-logo-fallback">${escHtml(init)}</span>`;
  }

  // The format of one member's own last test, for the mismatch pill's copy.
  //
  // `format_source` is set only when the check this table is scoped to and the member's
  // newest check disagree about the format. The verdict was decided on the newer one, so
  // that is the format the pill must name - reading the rendered test instead produced
  // "this member is 1920x1080 @ 60 and the group is pinned by hand to 1920x1080 @ 60"
  // (dev/docs/BUGS.md 2026-09-14).
  function memberFormatLabel(r) {
    const src = r.format_source;
    if (src) return src.label || src.measured || 'an unmeasured format';
    const t = r.last_test;
    if (!t || !t.resolution) return 'an unmeasured format';
    return t.fps ? `${t.resolution} @ ${Math.round(t.fps)}` : t.resolution;
  }

  // Where that other measurement came from, as one sentence to append to a row's format
  // copy. Names the check and the date, because "a different check said otherwise" that
  // does not say which check is not something anyone can go and look at.
  function formatSourceNote(r) {
    const src = r.format_source;
    if (!src) return '';
    const who = src.pre_check ? 'a recording pre-check'
      : (src.job_name ? `the ${src.job_name} check` : 'another check');
    const when = src.tested_at ? ` on ${src.tested_at}` : '';
    return ` This member's newest check is ${who}${when}, which measured`
      + ` ${src.measured} (${src.status}); the table is showing this check's result instead.`;
  }

  // A member proven to differ from the format a recording would open at if one started
  // right now, in a group whose format is FLOATING - no lock, so nothing is filtered and
  // 16.1's amber pill would be a lie ("It will not be used until it matches" promises an
  // enforcement that is not happening). What is true instead is 5.1's pin: a run starts on
  // the highest-ranked member and keeps that format, so a member reporting another one
  // cannot be failed over to for the rest of that run. The dimming is the same because the
  // meaning is the same - this member would not be used - and only the loudness differs.
  //
  // Three conditions carry the honesty:
  // - `best_format_known`: with no measured format for the member a run would START on,
  //   there is nothing for the pin to be, so nothing is proven about anybody.
  // - `!r.format_blocked` and `!format_override`: a lock in force owns this row's pill,
  //   and a lock that filtered everyone (15.2) is bypassed, so it excludes nobody.
  // - `r.mismatch` is never true for an untested member: unknown is not proven-different
  //   (5.1), and such a member stays eligible for failover, so it is never dimmed.
  function floatingMismatch(r) {
    return !!(WARN && WARN.is_source && WARN.best_format_known && G.hasChannel &&
      r.recording_enabled && r.mismatch && !r.format_blocked && !WARN.format_override);
  }

  // Everything questionable about ONE member's setup, as pills (16.1).
  //
  // Nothing here disables a control. Both switches are the user's and nothing automatic
  // moves either one, so these pills carry the entire explanation of why a member that is
  // switched on is not being used - an enabled control that currently does nothing is a
  // hair from a dead knob, and the pill plus its tooltip is what keeps it honest.
  //
  // A member with Recording OFF carries no pill at all: the switch one column over says
  // exactly that, and no format warning applies to a member that is not going to record.
  function rowWarnings(r) {
    if (!G.hasChannel || !r.recording_enabled) return [];
    const out = [];
    if (r.format_blocked && WARN && WARN.format_warns) {
      out.push({ cls: 'b-warn', pill: 'Format mismatch', title: 'Format mismatch.',
        detail: `Recording is on, but this member is ${memberFormatLabel(r)} and the group is ` +
          `pinned by hand to ${G.lockLabel || 'another format'}. It will not be used until it ` +
          'matches, or until you change the format strategy.' + formatSourceNote(r) });
    } else if (r.format_blocked) {
      // An automatic strategy's lock filters exactly as a pin does, so this member really is
      // skipped and the row still has to say why - but as a note, not a warning: the
      // strategy moving the group between formats is what it is for (dev/changelog/925).
      out.push({ note: true, pill: 'different format', title: 'Different format.',
        detail: `This member is ${memberFormatLabel(r)} and the group format is ` +
          `${G.lockLabel || 'another format'}, chosen by the format strategy. It is skipped ` +
          'when a recording picks a member until it matches, or until the strategy moves the ' +
          'group to its format.' + formatSourceNote(r) });
    } else if (floatingMismatch(r)) {
      // Quiet on purpose: nothing is wrong with this member being here, and it can become
      // the highest-ranked member itself - at which point the group format moves to ITS
      // format and the dimming inverts. A red pill would be crying wolf at a setup the
      // sweep case (7) calls the default shape of a new group.
      out.push({ note: true, pill: 'different format', title: 'Different format.',
        detail: `This member is ${memberFormatLabel(r)} and the group would record as ` +
          `${G.referenceLabel || 'another format'}, taken from its highest-ranked member. ` +
          'Nothing is filtered - this member can become the highest-ranked one itself - but a ' +
          'recording keeps the format it starts on, so a run beginning now would not fail ' +
          'over to it.' + formatSourceNote(r) });
    }
    // 4.3's two conditions, as two pills: they fail differently, and the second is the
    // more dangerous of the two because stale data is confidently wrong where missing
    // data is merely absent.
    if (!r.test_enabled) {
      const at = r.last_test && r.last_test.tested_at;
      if (!at) {
        out.push({ cls: 'b-warn', pill: 'Not monitored',
          title: 'Recording on, health check off - never tested.',
          detail: 'No health data. ChannelBin may select this member for a scheduled recording ' +
            'without knowing its format, its quality, or whether it works at all. Strongly ' +
            'recommended: run a health check on it first.' });
      } else {
        const days = Math.max(0, Math.floor((Date.now() - new Date(at + 'Z').getTime()) / 86400000));
        out.push({ cls: 'b-warn', pill: 'Not monitored',
          title: 'Recording on, health check off - data is going stale.',
          detail: `Last checked ${days === 0 ? 'today' : plural(days, 'day') + ' ago'}. ChannelBin ` +
            'will rank this on data that may no longer be true - it may be selected for a ' +
            'scheduled recording when we no longer know its current format, its current quality, ' +
            'or whether it still works. Turn its health check back on and give this group a ' +
            'health check schedule so it stays monitored.' });
      }
    }
    return out;
  }

  // One reader for "would a recording starting right now leave this member out", so the
  // desktop row and the phone card cannot dim different members. Both halves mean the same
  // thing and differ only in durability, which is what their pills say: the lock excludes a
  // member until the lock changes, the floating reference until the ranking moves.
  const rowDims = (r) => G.hasChannel && r.recording_enabled &&
    (r.format_blocked || floatingMismatch(r));

  // Every flag on one member, in one order, so the desktop name cell and the phone card
  // render the same set. A `note` entry is the quiet treatment (16.1's no-lock case) and a
  // badge is the loud one; nothing else separates them.
  function rowFlags(r) {
    return rowWarnings(r).map(w => w.note
      ? `<span class="gd-note tip-plain" data-tip="${escHtml(w.title)}&#10;${escHtml(w.detail)}">${escHtml(w.pill)}</span>`
      : `<span class="badge ${w.cls} gd-offpill tip-plain" data-tip="${escHtml(w.title)}&#10;${escHtml(w.detail)}">${escHtml(w.pill)}</span>`);
  }

  function nameCell(r) {
    const st = rowStatus(r);
    // The name dims when this member is switched ON for recording and would not be used by
    // a recording starting now, so the row reads as "present but not being used" before the
    // pill beside it is read. Only that state dims: a member the user switched off is a
    // deliberate choice, not a problem, and its own switch already says so.
    const dim = rowDims(r);
    let h = `<div class="gd-namecell">${logoHtml(r)}` +
      (fieldOn('account')
        ? `<span class="acct-dot tip-plain" style="background:${escHtml(r.account_color || '')}" data-tip="${escHtml(r.account_name || 'Account')}.&#10;Provider account this feed comes from."></span>`
        : '') +
      `<a href="/channels/${r.channel_id}" class="gd-name-link${dim ? ' dim' : ''}">${escHtml(r.channel_name)}</a>`;
    if (G.hasChannel && r.is_best && r.recording_enabled) {
      h += ` <span class="gd-best tip-plain" data-tip="Best eligible member.&#10;Highest health score among the members switched on for recording, so a recording starts here and fails over downward.">&#9733;</span>`;
    }
    h += '<span class="gd-flags">';
    if (r.disabled && st !== 'DISABLED') {
      h += `<span class="badge b-fail tip-plain" data-tip="Disabled.&#10;${escHtml(r.disabled_tip || '')}">&#10005; disabled</span>`;
    }
    // A member can hold more than one - "recording on, mismatched and unmonitored" is a
    // setup the app deliberately allows, so it renders as two pills rather than one
    // winning and hiding the other.
    h += rowFlags(r).join('');
    if (r.duplicate_title) {
      h += `<span class="badge b-warn tip-plain" data-tip="Duplicate feed.&#10;${escHtml(r.duplicate_title)}. Same stream URL, so it is literally the same feed listed twice - it adds nothing to failover and costs an extra test run.">&#10697; duplicate</span>`;
    }
    if (r.lifecycle === 'missing') {
      h += `<span class="badge b-warn tip-plain" data-tip="Missing.&#10;No longer seen in ${escHtml(r.account_name || 'this account')}'s synced feed since ${escHtml(r.lifecycle_date)}.">Missing ${escHtml(r.lifecycle_date)}</span>`;
    }
    // "No schedule reaches this feed" is a different fact from "this group's Health check
    // switch is off", but they read as the same words - so the note is suppressed when a
    // 16.1 pill is already saying it louder and with the reason attached.
    const pillSaysUnmonitored = G.hasChannel && r.recording_enabled && !r.test_enabled;
    if (G.hasChannel && !r.monitored && !pillSaysUnmonitored) {
      h += `<span class="gd-note tip-plain" data-tip="Not monitored.&#10;No recurring health check covers this feed, so format drift will not be caught automatically.">not monitored</span>`;
    }
    // The honest third state (DECIDED 5): joining a group no longer hides a channel, so a
    // member may carry its own guide row alongside the group's and this says which do.
    if (r.in_guide) {
      h += `<span class="gd-note tip-plain" data-tip="Its own guide row.&#10;This channel appears in the TV Guide on its own as well as through this group. Joining a group does not hide it.">also its own guide row</span>`;
    }
    h += '</div>';
    return h;
  }

  // One participation switch. It is NEVER disabled: the format lock filters where members
  // are chosen and writes nothing, so a member the lock is currently skipping keeps a live
  // control and the pill beside its name explains why it is not being used.
  function partCell(r, which) {
    const on = which === 'rec' ? r.recording_enabled : r.test_enabled;
    const label = COL_LABEL[which];
    const tip = COL_TIP[which];
    return `<td class="gd-c-part"><span class="gd-part">` +
      `<label class="switch tip-plain" data-tip="${tip}">` +
      `<input type="checkbox" data-part="${which}" data-cid="${r.channel_id}"${on ? ' checked' : ''}` +
      ` aria-label="${escHtml(label)} for ${escHtml(r.channel_name)}">` +
      `<span class="knob"></span></label></span></td>`;
  }

  function cell(r, k) {
    const t = r.last_test;
    if (PART_COLS[k]) return partCell(r, k);
    switch (k) {
      case 'select':
        return `<td class="gd-c-select">${r.disabled ? '' :
          `<input type="checkbox" data-sel="${r.channel_id}"${selected.has(r.channel_id) ? ' checked' : ''} aria-label="Select ${escHtml(r.channel_name)}">`}</td>`;
      case 'caret':
        return `<td class="gd-c-caret">${canExpand(r)
          ? `<span class="gd-caret" data-expand="${r.channel_id}" data-tip="Stream profile.&#10;Codec, bit depth, chroma, scan type, frame-rate stability and the screenshot from the last test. Clicking anywhere on the row does the same thing."></span>`
          : ''}</td>`;
      case 'name': return `<td>${nameCell(r)}</td>`;
      case 'status': return `<td>${statusBadge(r)}</td>`;
      case 'score': return `<td>${scoreCell(r)}</td>`;
      case 'res': {
        if (!t || !t.resolution) return '<td class="num"><span class="text-muted">&mdash;</span></td>';
        // FPS is repeated here as a subtitle so Format reads at a glance without the
        // separate FPS column; sorting still keys off resolution alone.
        const sub = t.fps ? `<div class="gd-sub num">${t.fps.toFixed(1)} fps</div>` : '';
        // The cell keeps rendering THIS check's measurement - the table's job scope is the
        // feature, and the Format filter buckets on the same value, so the column and the
        // filter cannot drift apart. What it may not do is state the value flat while the
        // row's format verdict was reached on another one: that is the contradiction the
        // tooltip exists to name (dev/docs/BUGS.md 2026-09-14).
        if (r.format_source) {
          return `<td class="num"><span class="val-warn tip-plain" data-tip="A newer check disagrees.` +
            `&#10;${escHtml(formatSourceNote(r).trim())}">${escHtml(t.resolution)}</span>${sub}</td>`;
        }
        return `<td class="num">${escHtml(t.resolution)}${sub}</td>`;
      }
      case 'fps': return `<td class="num">${t && t.fps ? t.fps.toFixed(1) : '<span class="text-muted">&mdash;</span>'}</td>`;
      case 'audio': {
        if (!t || !t.audio_codec) return '<td><span class="text-muted">&mdash;</span></td>';
        // Language rides as the subtitle rather than a fourth term on one line: it is the
        // one audio fact that differs between members of a group that otherwise match, so
        // it needs to be readable at a glance rather than buried at the end of a string.
        const sub = t.audio_language ? `<div class="gd-sub">${escHtml(t.audio_language)}</div>` : '';
        return `<td>${escHtml(t.audio_summary || t.audio_codec.toUpperCase())}${sub}</td>`;
      }
      case 'framePct': {
        if (!t || t.frame_pct === null || t.frame_pct === undefined) return '<td class="num text-muted">&mdash;</td>';
        const cls = t.frame_pct >= 95 ? 'val-good' : (t.frame_pct >= 80 ? 'val-warn' : 'val-bad');
        // Derived from the stored pair, never recomputed as fps x duration: the server
        // measures the expected count over the clip's decode span, and a second formula
        // here would print a total that contradicts the percentage beside it
        // (dev/changelog/896).
        const expected = t.frame_pct > 0 ? Math.round((t.frame_count || 0) / t.frame_pct * 100) : 0;
        return `<td class="num ${cls} tip-plain" data-tip="Frame delivery ${t.frame_pct.toFixed(1)}%.&#10;${t.frame_count || 0} frames received of ${expected} expected over ${Math.round(t.duration_seconds || 0)}s captured. Below 100% means brief stalls or dropped frames during the test itself.">${t.frame_pct.toFixed(1)}%</td>`;
      }
      case 'bitrate': return `<td class="num">${bitrateCell(t)}</td>`;
      case 'drops': {
        if (!t || t.drop_count === null || t.drop_count === undefined) return '<td class="num text-muted">&mdash;</td>';
        const d = t.drop_count || 0;
        const cls = d === 0 ? '' : (d <= 2 ? 'val-warn' : 'val-bad');
        return `<td class="num ${cls}">${d}</td>`;
      }
      case 'shot': return `<td>${shotCell(t)}</td>`;
      case 'epg': {
        if (!r.epg_channel_id) return '<td><span class="text-muted">&mdash;</span></td>';
        // Truncated on an inner span rather than on the <td>: the header cell of a sortable
        // column carries `sortable` instead of a `gd-c-*` class, so a width put on the body
        // cell alone is a coupled value with nothing holding the two ends together.
        return `<td><span class="gd-epg-id tip-plain" data-tip="EPG id.&#10;${escHtml(r.epg_channel_id)}">` +
          `${escHtml(r.epg_channel_id)}</span></td>`;
      }
      case 'acts':
        return `<td class="gd-c-acts"><span class="gd-rowacts">` +
          `<a href="/channels/${r.channel_id}" class="btn btn-sm">Details</a>` +
          `<button class="btn btn-sm btn-icon" data-rowmenu="${r.channel_id}" aria-label="More actions">&#8943;</button>` +
          `</span></td>`;
      default: return '<td></td>';
    }
  }

  // duplicated from app/fmt_utils.py (fmt_chroma + the *_TIP constants) - this drawer is
  // rebuilt client-side after every poll, so the labels and tooltip prose cannot come from
  // the server-rendered copies the recording and channel detail pages use. Change one, change
  // both: the same measurement must read the same on all three surfaces (dev/changelog/351).
  function chroma(v) { return v ? v.split('').join(':') : null; }

  function drawer(r) {
    const t = r.last_test;
    const shot = t && t.screenshot_filename && !t.screenshot_pruned
      ? `<div class="gd-q-shot"><img class="gd-shot-lg" src="/channel-tests/screenshots/${encodeURIComponent(t.screenshot_filename)}" alt="Last test screenshot" data-shot="/channel-tests/screenshots/${encodeURIComponent(t.screenshot_filename)}">` +
        `<div class="gd-shot-cap">Last test${t.started_et ? ' - ' + escHtml(t.started_et) : ''}</div></div>`
      : `<div class="gd-q-shot"><div class="gd-shot-lg none">No screenshot</div>` +
        `<div class="gd-shot-cap">${rowStatus(r) === 'FAIL' ? 'Feed never produced video.' : 'Not captured on this run.'}</div></div>`;

    const item = (label, value, cls, tip) =>
      `<div class="gd-q-item"><div class="gd-q-lbl">${label}</div>` +
      `<div class="gd-q-val${cls ? ' ' + cls : ''}${tip ? ' tip-plain' : ''}"${tip ? ` data-tip="${label}.&#10;${tip}"` : ''}>${value}</div></div>`;

    // Built once and appended by BOTH branches below. A test can carry audio without a video
    // profile, and the no-video branch used to return before any audio was rendered - so the
    // fix to canExpand() alone would only have opened a drawer that still threw it away
    // (dev/changelog/769). Each field is conditional rather than dashed: an absent bitrate or
    // language is not a measurement worth a row of its own.
    function audioItems() {
      if (!t || !t.audio_codec) return '';
      let out = item('Audio', escHtml(t.audio_summary || t.audio_codec.toUpperCase()));
      if (t.audio_bitrate_kbps) out += item('Audio bitrate', `${Math.round(t.audio_bitrate_kbps)} kbps`);
      if (t.audio_language) out += item('Language', escHtml(t.audio_language), '',
        'The audio track\'s declared language, as the provider tagged it.');
      return out;
    }

    if (!t || !t.video_codec) {
      const audio = audioItems();
      // Three readings of "no video profile", and they are not interchangeable. A failed
      // test keeps its own error whatever else was measured; a test that measured audio did
      // not predate profile capture, so saying it did would be false; only the third is the
      // original pre-capture case. The explanation stays even when audio is present - "no
      // video profile" is the thing being disclosed, and dropping it would leave a drawer
      // that reads as complete.
      let why;
      if (rowStatus(r) === 'FAIL') {
        why = `This test failed before any stream was probed${t && t.error_detail ? ': ' + escHtml(t.error_detail) : '.'}`;
      } else if (audio) {
        why = 'No video profile was recorded for this test, so codec, bit depth and chroma are missing. Its audio was measured:';
      } else {
        why = 'This test predates stream-profile capture, so codec, bit depth and chroma were not recorded.';
      }
      return `<div class="gd-q">${shot}<div class="gd-q-grid">` +
        `<div class="gd-q-empty">${why}</div>${audio}</div></div>`;
    }

    let grid = '';
    grid += item('Codec', escHtml(t.video_codec));
    grid += item('Bit depth', t.bit_depth ? `${t.bit_depth}-bit` : '&mdash;');
    grid += item('Chroma', escHtml(chroma(t.chroma_subsampling) || '-'), '',
      'Chroma subsampling. 4:2:2 keeps more color detail than the usual 4:2:0; some players and remuxers handle it differently.');
    if (t.interlaced === null || t.interlaced === undefined) grid += item('Scan', 'Unknown');
    else if (t.interlaced) grid += item('Scan', 'Interlaced', 'val-warn',
      'Interlaced source. Some players deinterlace it, some do not - the recording may show combing.');
    else grid += item('Scan', 'Progressive', '', 'Progressive - one full frame at a time.');
    if (t.is_vfr === null || t.is_vfr === undefined) grid += item('Frame rate', t.fps ? `${t.fps.toFixed(1)} fps` : '&mdash;');
    else if (t.is_vfr) grid += item('Frame rate', 'Variable', 'val-warn',
      'Variable frame rate: the declared and average frame rates disagree. Can drift audio sync in a long recording.');
    else grid += item('Frame rate', `${t.fps ? t.fps.toFixed(1) : '?'} fps constant`);
    if (t.bits_per_pixel_frame) grid += item('Efficiency', t.bits_per_pixel_frame.toFixed(4), '',
      'Bits per pixel per frame = bitrate / (width x height x fps). A standard compression-efficiency stat, not a better/worse verdict.');
    if (t.coded_resolution) grid += item('Coded size', escHtml(t.coded_resolution), '',
      'The encoder\'s padded frame size, which differs from the displayed resolution.');
    grid += audioItems() || item('Audio', '&mdash;');
    if (t.timeline_gap_count) {
      grid += item('Timeline gaps', `${t.timeline_gap_count} (${(t.timeline_gap_seconds || 0).toFixed(1)}s)`,
        (t.timeline_gap_seconds || 0) > 1 ? 'val-bad' : 'val-warn',
        'Presentation-time gaps found in the test clip - the source dropped or paused mid-stream.');
    }

    let flags = '';
    if (t.interlaced) flags += '<span class="badge b-warn">Interlaced</span>';
    if (t.is_vfr) flags += '<span class="badge b-warn">VFR</span>';
    if (t.timeline_gap_count) flags += `<span class="badge b-fail">${t.timeline_gap_count} timeline gap${t.timeline_gap_count === 1 ? '' : 's'}</span>`;
    if (!flags) flags = '<span class="h-mini ok">&#10003; Clean profile</span>';

    return `<div class="gd-q">${shot}<div class="gd-q-grid">${grid}<div class="gd-q-flags">${flags}</div></div></div>`;
  }

  function renderTable() {
    const cols = columns();
    byId('gd-thead').innerHTML = '<tr>' + cols.map(c => {
      if (!c.sort) return `<th class="gd-c-${c.k}">${escHtml(c.label)}</th>`;
      const on = sortKey === c.k;
      // A participation header keeps its own tooltip rather than the generic sort one:
      // "Recording" and "Health check" each need saying what the switch under them does,
      // and the sort hint is appended to it instead of replacing it.
      const tip = c.tip
        ? `${c.tip} Click to sort by it; click again to reverse.`
        : `Sort by ${escHtml(c.label)}.&#10;Click to sort; click again to reverse. Untested channels sort to the bottom either way.`;
      return `<th class="sortable${on ? ' sorted' : ''}${PART_COLS[c.k] ? ` gd-c-${c.k} gd-c-part` : ''}" data-sort="${c.k}"` +
        ` data-tip="${tip}">` +
        `${escHtml(c.label)}<span class="arr">${on ? (sortDir < 0 ? ' ▾' : ' ▴') : ''}</span></th>`;
    }).join('') + '</tr>';

    const rows = visibleRows();
    const tbody = byId('gd-tbody');
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="${cols.length}" class="gd-empty">No channels match.</td></tr>`;
    } else {
      tbody.innerHTML = rows.map(r => {
        const cls = 'gd-row' + (r.disabled ? ' is-disabled' : '') +
          (expanded.has(r.channel_id) ? ' expanded' : '') + (canExpand(r) ? '' : ' no-profile');
        let out = `<tr class="${cls}" data-cid="${r.channel_id}">${cols.map(c => cell(r, c.k)).join('')}</tr>`;
        if (expanded.has(r.channel_id)) {
          out += `<tr class="gd-drawer-row"><td colspan="${cols.length}">${drawer(r)}</td></tr>`;
        }
        return out;
      }).join('');
    }
    byId('gd-row-count').textContent = `Showing ${rows.length} of ${TOTAL}`;
  }

  // ══════════════════════════════════════════════════════════════════════════
  // THE PHONE ARRANGEMENT (dev/mockups/34-group-detail-mobile.html, round 2.1;
  // ported per DESIGN-channel-groups-model.md §17, dev/changelog/758)
  //
  // ONE module draws both widths. The state above - the rows, the filters, the sort, the
  // selection, every write path - is shared verbatim, and only the DRAWING branches. A
  // second template would be a second copy of all of it, and the two would disagree
  // (static/js/channel-search.js made the same call for the same reason).
  //
  // matchMedia and nothing else, so there is one spelling of 768 in this file to match the
  // one in style.css. jsdom answers `matches: false` always, which is why the desktop
  // drawing is what the suite sees and why the browser pass is not optional here.
  // ══════════════════════════════════════════════════════════════════════════

  const MOBILE_MQ = window.matchMedia('(max-width: 768px)');
  const isPhone = () => MOBILE_MQ.matches;

  // Which of the shared fields a card can draw, in `colState.order`'s order so the phone
  // and the desktop agree about what is on. `fps` is deliberately absent: the Format field
  // already carries it as a suffix, and on one wrapping line a bare number is unreadable.
  const CARD_FIELDS = ['status', 'score', 'res', 'audio', 'bitrate', 'framePct', 'drops', 'epg'];

  // The two participation switches are never in the field picker - they are what the page
  // exists to set - and neither is the name or anything explaining why a member is flagged.
  const PICKABLE_FIELDS = () => colState.order.filter(k => CARD_FIELDS.includes(k) || FIELD_ONLY[k]);

  // The stats the desktop puts in columns, on one line. Never truncated and never a fixed
  // track: a card line is not a table track, so there is no header to slide off it.
  function statLine(r) {
    const t = r.last_test;
    const bits = [];
    colState.order.filter(k => CARD_FIELDS.includes(k) && fieldOn(k)).forEach(k => {
      if (k === 'status') return;               // drawn as a badge in the card's top row
      if (k === 'score') {
        const s = rowScore(r);
        bits.push(s === null
          ? '<span class="text-muted">no score</span>'
          : `<span class="hb-text ${healthBandCss(s)}">&#9733;${s}</span>`);
      } else if (k === 'res') {
        const fmt = t && t.resolution
          ? escHtml(t.resolution) + (t.fps ? ` @ ${t.fps.toFixed(0)}` : '')
          : null;
        if (fmt && r.format_source) {
          // Same disclosure the desktop Format cell carries - this line is where the
          // contradiction was actually reported from (dev/docs/BUGS.md 2026-09-14).
          bits.push(`<span class="val-warn tip-plain" data-tip="A newer check disagrees.` +
            `&#10;${escHtml(formatSourceNote(r).trim())}">${fmt}</span>`);
        } else {
          bits.push(fmt || '<span class="text-muted">no format</span>');
        }
      } else if (k === 'audio') {
        // The summary plus the language, which is the pair the desktop cell shows on its two
        // lines - a card line has no second line to put the subtitle on.
        if (t && t.audio_codec) {
          bits.push(escHtml(t.audio_summary || t.audio_codec.toUpperCase()) +
            (t.audio_language ? ` (${escHtml(t.audio_language)})` : ''));
        }
      } else if (k === 'bitrate') {
        bits.push(bitrateCell(t));
      } else if (k === 'framePct' && t && t.frame_pct !== null && t.frame_pct !== undefined) {
        bits.push(`${t.frame_pct.toFixed(1)}% frames`);
      } else if (k === 'drops' && t && t.drop_count !== null && t.drop_count !== undefined) {
        bits.push(plural(t.drop_count, 'drop'));
      } else if (k === 'epg' && r.epg_channel_id) {
        // Labeled, unlike every other bit on this line: a bare provider id beside a
        // resolution and a bitrate reads as neither, and the phone has no column header
        // above it to say what it is.
        bits.push(`EPG ${escHtml(r.epg_channel_id)}`);
      }
    });
    if (!bits.length) return '';
    return `<div class="gm-stats num">${bits.join('<span class="gm-sep">&middot;</span>')}</div>`;
  }

  // The status band down a card's left edge, keyed on the same `data-health` attribute and
  // the same five values `.grp-item` and the recordings rows already use, except TESTING:
  // that one is 'st-run' (blue), not 'st-live' (the recording-red alarm colour) - a channel
  // being tested is not a failure, and reusing the recording token made it read as one
  // (dev/changelog/816). It is the LAST HEALTH CHECK's verdict - the same thing the status
  // badge says - readable while scrolling, not a second opinion. An untested member gets the
  // faint band rather than none, because untested is a state and never an assumption that a
  // feed is good.
  function healthBand(r) {
    switch (rowStatus(r)) {
      case 'PASS': return 'st-ok';
      case 'WARN': return 'st-warn';
      case 'TESTING': return 'st-run';
      case 'WAITING': case 'CANCELLED': case 'DISABLED': return 'st-none';
      default: return 'st-bad';
    }
  }

  // One participation switch on its own labeled row. Same shared `.switch` the desktop
  // column uses and the same never-disabled contract (§4.1) - what changes is that the row
  // carries the label, because there is no column header above it to answer it.
  function partRow(r, which) {
    const on = which === 'rec' ? r.recording_enabled : r.test_enabled;
    const label = COL_LABEL[which];
    const tip = COL_TIP[which];
    return `<label class="gm-part tip-plain" data-tip="${tip}">` +
      `<span class="gm-part-lbl">${escHtml(label)}</span>` +
      `<span class="switch"><input type="checkbox" data-part="${which}" data-cid="${r.channel_id}"` +
      `${on ? ' checked' : ''} aria-label="${escHtml(label)} for ${escHtml(r.channel_name)}">` +
      `<span class="knob"></span></span></label>`;
  }

  // One member, one card. Every fact the desktop row carries survives: the name takes its
  // own wrapping line (DESIGN.md 9.4), the stats collapse onto one line, and the two
  // switches move onto their own labeled rows.
  function memberCard(r) {
    const t = r.last_test;
    const st = rowStatus(r);
    let h = `<div class="gm-card${r.disabled ? ' is-disabled' : ''}` +
      `${selecting && selected.has(r.channel_id) ? ' sel' : ''}"` +
      ` data-health="${healthBand(r)}" data-cid="${r.channel_id}">`;

    h += '<div class="gm-top">';
    if (selecting && !r.disabled) {
      h += `<input type="checkbox" class="gm-check" data-sel="${r.channel_id}"` +
        `${selected.has(r.channel_id) ? ' checked' : ''} aria-label="Select ${escHtml(r.channel_name)}">`;
    }
    const dim = rowDims(r);
    h += `${logoHtml(r)}<div class="gm-headline">` +
      `<a href="/channels/${r.channel_id}" class="gm-name gd-name-link${dim ? ' dim' : ''}">${escHtml(r.channel_name)}</a>`;
    if (G.hasChannel && r.is_best && r.recording_enabled) {
      h += ` <span class="gd-best tip-plain" data-tip="Best eligible member.&#10;Highest health score among the members switched on for recording, so a recording starts here and fails over downward.">&#9733;</span>`;
    }
    if (fieldOn('account')) {
      h += `<div class="gm-acct"><span class="acct-dot" style="background:${escHtml(r.account_color || '')}"></span>` +
        `${escHtml(r.account_name || 'Account')}</div>`;
    }
    h += '</div>';
    if (fieldOn('status')) h += `<span class="gm-status">${statusBadge(r)}</span>`;
    h += '</div>';

    // Same pills, same copy and same order as the desktop name cell - they are built from
    // rowFlags(), so there is one authority for what a member is flagged for.
    const flags = [];
    if (r.disabled && st !== 'DISABLED') {
      flags.push(`<span class="badge b-fail tip-plain" data-tip="Disabled.&#10;${escHtml(r.disabled_tip || '')}">&#10005; disabled</span>`);
    }
    flags.push(...rowFlags(r));
    if (r.duplicate_title) {
      flags.push(`<span class="badge b-warn tip-plain" data-tip="Duplicate feed.&#10;${escHtml(r.duplicate_title)}. Same stream URL, so it is literally the same feed listed twice - it adds nothing to failover and costs an extra test run.">&#10697; duplicate</span>`);
    }
    if (r.lifecycle === 'missing') {
      flags.push(`<span class="badge b-warn tip-plain" data-tip="Missing.&#10;No longer seen in ${escHtml(r.account_name || 'this account')}'s synced feed since ${escHtml(r.lifecycle_date)}.">Missing ${escHtml(r.lifecycle_date)}</span>`);
    }
    const pillSaysUnmonitored = G.hasChannel && r.recording_enabled && !r.test_enabled;
    if (G.hasChannel && !r.monitored && !pillSaysUnmonitored) {
      flags.push(`<span class="gd-note tip-plain" data-tip="Not monitored.&#10;No recurring health check covers this feed, so format drift will not be caught automatically.">not monitored</span>`);
    }
    if (r.in_guide) {
      flags.push(`<span class="gd-note tip-plain" data-tip="Its own guide row.&#10;This channel appears in the TV Guide on its own as well as through this group. Joining a group does not hide it.">also its own guide row</span>`);
    }
    if (flags.length) h += `<div class="gm-flags">${flags.join('')}</div>`;

    h += statLine(r);
    if (G.hasChannel) h += `<div class="gm-parts">${partRow(r, 'rec')}${partRow(r, 'test')}</div>`;

    // "Info", not "Stream profile": three things on this page are within a word of each
    // other - the stream profile, the recording profile and the test profile - and only
    // this one is a per-channel disclosure rather than a setting.
    h += '<div class="gm-acts">' +
      (canExpand(r)
        ? `<button class="btn" data-expand="${r.channel_id}">${expanded.has(r.channel_id) ? 'Hide info' : 'Info'}</button>`
        : '') +
      `<a href="/channels/${r.channel_id}" class="btn">Details</a>` +
      `<button class="btn btn-icon" data-rowmenu="${r.channel_id}" aria-label="More actions">&#8943;</button>` +
      '</div>';
    if (expanded.has(r.channel_id)) h += `<div class="gm-drawer">${drawer(r)}</div>`;
    return h + '</div>';
  }

  function renderCards() {
    const rows = visibleRows();
    byId('gd-list').innerHTML = rows.length
      ? rows.map(memberCard).join('')
      : '<div class="empty-state">No channels match.</div>';
  }

  // The single entry point every caller uses. Which of the two drawings runs is the only
  // thing the breakpoint decides; the one that is not drawn is emptied rather than left
  // holding a stale second copy of the list.
  function renderList() {
    const phone = isPhone();
    if (phone) {
      byId('gd-thead').innerHTML = '';
      byId('gd-tbody').innerHTML = '';
      renderCards();
    } else {
      byId('gd-list').innerHTML = '';
      renderTable();
    }
    // The count reads plain until something is filtering, and only then says what it is
    // out of - a permanent "6 of 6" is noise. On a desktop the bulk bar's own
    // "Showing N of M" already answers it, so the head stays the total there.
    const shown = visibleRows().length;
    byId('gd-chan-count').textContent = (phone && shown !== TOTAL) ? `${shown} of ${TOTAL}` : TOTAL;
    renderChipsRow();
    syncSelection();
  }

  // ── The phone's chrome: three chips, four sheets, one bottom bar ──────────
  // A sheet IS a modal - style.css turns `.modal-panel` into a bottom sheet at this width
  // (DESIGN.md 9.6), so there is no second overlay component here: no bespoke scrim, no
  // second scroll lock, no second Escape handler. util.js owns all of it.
  let sheetEl = null;
  let sheetRedraw = null;
  // `redraw` is held here rather than read back off the DOM for the same reason the row
  // state is: a sheet's body is rewritten on every tap, and a handler found by querying
  // the document afterwards is a second source of truth for which sheet is open.
  function sheet(overlay, redraw) {
    sheetEl = overlay;
    sheetRedraw = redraw || null;
    return overlay;
  }
  function closeSheet() {
    if (sheetEl && sheetEl.closeModal) sheetEl.closeModal();
    sheetEl = null;
    sheetRedraw = null;
  }

  const SORTS = [
    ['score', 'Health score'], ['name', 'Name'], ['status', 'Status'],
    ['rec', 'Recording'], ['test', 'Health check'], ['res', 'Format'], ['bitrate', 'Bitrate'],
  ];
  const sortLabel = () => (SORTS.find(([k]) => k === sortKey) || SORTS[0])[1];

  function renderChipsRow() {
    const sortChip = byId('gd-chip-sort');
    if (!sortChip) return;
    // The sortable column headers go away with the table, so this chip is the only thing
    // left saying what the list is ordered by and in which direction (DESIGN.md 9.4).
    sortChip.innerHTML = `Sort: ${escHtml(sortLabel())} ` +
      `<span class="gm-dir">${sortDir < 0 ? '&#9662;' : '&#9652;'}</span>`;
    // The desktop draws one chip per active filter; at this width they collapse into the
    // count on this one chip, which is what replaces being able to see them all at once.
    const n = filterBar.count();
    const filterChip = byId('gd-chip-filter');
    filterChip.innerHTML = n ? `Filters <span class="gm-count">${n}</span>` : 'Filters';
    filterChip.className = `chip${n ? ' active' : ''}`;
    const selectChip = byId('gd-chip-select');
    if (selectChip) {
      selectChip.textContent = selecting ? 'Done' : 'Select';
      selectChip.className = `chip${selecting ? ' active' : ''}`;
    }
  }

  function setSelecting(on) {
    selecting = on;
    // Leaving the mode clears the selection rather than leaving it armed and invisible.
    if (!on) selected.clear();
    renderList();
  }

  // A sheet row. The shared `.sheet-act` (style.css), so the accounts pages and this one
  // draw one component; `.gm-tick` is what a row uses to say it is the active choice,
  // which is what a menu would have used a check column for. `danger` is the same
  // destructive-red the overflow sheet already gives a cloned kebab item.
  const sheetRow = (attrs, label, mark, danger) =>
    `<button class="sheet-act${danger ? ' danger' : ''}" ${attrs}><span>${escHtml(label)}</span>` +
    `${mark ? `<span class="gm-tick">${mark}</span>` : ''}</button>`;

  // Every field is bidirectional: tapping the one already active flips its direction.
  function openSort() {
    const body = document.createElement('div');
    const draw = () => {
      body.innerHTML = SORTS.map(([k, label]) => sheetRow(
        `data-sortpick="${k}"`, label,
        sortKey === k ? (sortDir < 0 ? 'High to low' : 'Low to high') : '')).join('');
    };
    draw();
    return sheet(buildModal({
      title: 'Sort by', body,
      footer: [{ label: 'Close', class: 'btn', onClick: (c) => c() }],
    }), draw);
  }

  // The desktop's + Filter popover plus the field picker, in one sheet. A popover that
  // has to be drilled into is a bad control on a phone, so this stays a sheet - but it is
  // drawn from the same FILTER_DIMS registry and writes the same state, so the two can
  // never disagree about what is on. The chip's count is what replaces seeing the chips.
  function openFilters() {
    const body = document.createElement('div');
    const draw = () => {
      let h = '';
      FILTER_DIMS.forEach(d => {
        if (d.available && !d.available()) return;
        const vals = typeof d.values === 'function' ? d.values() : d.values;
        if (!vals.length) return;
        h += `<div class="gm-sheet-sec">${escHtml(d.label)}</div>` +
          vals.map(o => sheetRow(
            `data-fpick="${escHtml(d.k)}" data-fpickval="${escHtml(o.v)}"`,
            o.label, filterBar.has(d.k, o.v) ? '&#10003;' : '')).join('');
      });
      // What the desktop Columns popover is at this width. DESIGN.md 9.4's 2026-07-30
      // amendment allows a phone a field-VISIBILITY list but never the popover: a card
      // line is not a table track, so there is no header to slide off and nothing to
      // reorder. The set is the shared `colState`, so a field hidden here is hidden on the
      // desktop too - which is the point for anyone with a single account.
      h += '<div class="gm-sheet-sec">Fields on each card</div>' +
        PICKABLE_FIELDS().map(k =>
          sheetRow(`data-fieldpick="${k}"`, COL_LABEL[k], fieldOn(k) ? '&#10003;' : '')).join('');
      body.innerHTML = h;
    };
    draw();
    return sheet(buildModal({
      title: 'Filters', body,
      footer: [
        { label: 'Clear all', class: 'btn', onClick: () => { filterBar.clear(); afterFilterChange(); } },
        { label: 'Done', class: 'btn btn-primary', onClick: (c) => c() },
      ],
    }), draw);
  }

  // The sheet behind it, the desktop chips and the chip's count all update on every tap,
  // so a filter cannot be left on invisibly - and the popover is redrawn too, because a
  // breakpoint change must not land on a control still showing the state it had before.
  // `fromBar` is set when the bar itself made the change and has already redrawn.
  function afterFilterChange(fromBar) {
    if (sheetRedraw) sheetRedraw();
    if (!fromBar) filterBar.render();
    renderList();
  }

  // The sort sheet redraws in place too, so the direction line under the active field
  // moves as you tap rather than only once the sheet is dismissed.
  function afterSortChange() {
    if (sheetRedraw) sheetRedraw();
    renderList();
  }

  // The overflow sheet is built from the desktop kebab's OWN children, exactly as
  // static/js/accounts.js does: the action list is Jinja-rendered once and read from there,
  // so a phone cannot be missing an action the desktop offers. The two header buttons that
  // do not fit the bar are prepended.
  function openOverflow() {
    const body = document.createElement('div');
    const extra = Array.from(document.querySelectorAll('[data-section="channels"] .card-head-actions .btn'))
      .filter(el => el.dataset.act === 'suggest' || el.tagName === 'A');
    const render = (el) => {
      if (el.classList.contains('sep')) return '<div class="sheet-sep"></div>';
      const label = escHtml(el.textContent.trim());
      if (el.tagName === 'A') return `<a class="sheet-act" href="${escHtml(el.getAttribute('href'))}">${label}</a>`;
      return `<button class="sheet-act${el.classList.contains('danger') ? ' danger' : ''}"` +
        ` data-act="${escHtml(el.dataset.act || '')}"${el.disabled ? ' disabled' : ''}>${label}</button>`;
    };
    const kebab = byId('gd-kebab');
    body.innerHTML = extra.map(render).join('') +
      (extra.length ? '<div class="sheet-sep"></div>' : '') +
      (kebab ? Array.from(kebab.children).map(render).join('') : '');
    const overlay = sheet(buildModal({
      title: 'More actions', body,
      footer: [{ label: 'Close', class: 'btn', onClick: (c) => c() }],
    }));
    body.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn || btn.disabled) return;
      // Close first: the sheet's markup goes with it, and the action then runs against a
      // document holding exactly one copy of everything.
      closeSheet();
      pageAction(btn.dataset.act, btn);
    });
    return overlay;
  }

  // Which of the current selection the server already flagged missing (r.lifecycle,
  // set by group_detail_rows() from the same lifecycle lookup the banner counts from) -
  // the set openDeleteMissingSelected() acts on and the bulk sheet uses to decide
  // whether that row is worth offering at all.
  function selectedMissingIds() {
    return Array.from(selected).filter((id) => {
      const r = rowById(id);
      return r && r.lifecycle === 'missing';
    });
  }

  // The bulk verbs, as a sheet rather than the desktop's two dropdown buttons. Same
  // `data-bulk` values, so they reach the same one route and the same one writer.
  function openBulkSheet() {
    const n = selected.size;
    const body = document.createElement('div');
    const forN = (dir) => `Turn ${dir} for ${plural(n, 'member')}`;
    let h = '';
    if (G.hasChannel) {
      h += `<div class="gm-sheet-sec">${escHtml(COL_LABEL.rec)}</div>` +
        sheetRow('data-bulk="rec:on"', forN('on')) + sheetRow('data-bulk="rec:off"', forN('off')) +
        `<div class="gm-sheet-sec">${escHtml(COL_LABEL.test)}</div>` +
        sheetRow('data-bulk="test:on"', forN('on')) + sheetRow('data-bulk="test:off"', forN('off'));
    }
    if (G.hasCheck) {
      h += '<div class="gm-sheet-sec">Health check run</div>' +
        sheetRow('data-act="test-selected"', `Test the ${n} selected now`);
    }
    // Deleting a channel is unrelated to group/check membership - it is the same
    // permanent action the kebab's own "Delete missing channels..." runs group-wide,
    // scoped here to just the selection (dev/changelog/653, dev/changelog/772's
    // channel_ids path). Only offered when the selection actually contains one, so the
    // row never dead-ends into "None of the selected channels are missing."
    const missingIds = selectedMissingIds();
    if (missingIds.length) {
      h += '<div class="gm-sheet-sec">Missing channels</div>' +
        sheetRow('data-act="delete-missing-selected"',
          `Delete ${plural(missingIds.length, 'missing channel')}…`, null, true);
    }
    body.innerHTML = h;
    return sheet(buildModal({
      title: `Apply to ${n} selected`, body,
      footer: [{ label: 'Cancel', class: 'btn', onClick: (c) => c() }],
    }));
  }

  // ── The sticky bottom bar and its reveal ─────────────────────────────────
  // Two states in ONE bar: the group's own actions, and - in selection mode - the verbs
  // that act on the selection. A phone has no room for two stacked bars, and a bar that
  // changes what it acts on is exactly what the Select chip announces.
  //
  // The group's actions are CLONED from the Jinja-rendered inline `.gd-ab-actions` rather
  // than re-rendered here. The accounts page shares its primary between its two bars
  // through one macro for the same reason: the guide button's label changes with state,
  // and two copies of a control that changes is two controls that can disagree.
  function renderBottomBar() {
    const bar = byId('gd-bottombar');
    if (!bar) return;
    if (selecting) {
      const n = selected.size;
      bar.innerHTML = `<span class="gm-selcount">${n} selected</span>` +
        '<button class="btn" data-act="select-all-visible">All</button>' +
        `<button class="btn btn-primary" data-act="bulk-sheet"${n ? '' : ' disabled'}>Actions &#9652;</button>` +
        '<button class="btn btn-icon" data-act="select-done" aria-label="Leave selection mode">&times;</button>';
    } else {
      bar.innerHTML = '';
      const inline = byId('gd-inline-actions');
      if (inline) {
        Array.from(inline.children).forEach(el => {
          // The kebab is not cloned: it would duplicate `id="gd-kebab"` into the document,
          // and a menu anchored to a bottom-pinned button would open downward off screen
          // anyway. Its contents are what the overflow sheet reads.
          if (el.classList.contains('menu-wrap')) return;
          // A control the inline bar drops at this width is not smuggled into the sticky
          // one. Cloning all of them put five buttons in a 375px bar, which scrolled
          // sideways with two reachable and three found only by swiping a bar nothing says
          // is scrollable. `.mobile-actbar`'s `flex: 1` cannot divide a fixed width among
          // an unbounded number of buttons, so the fix is the number, not the CSS.
          if (el.classList.contains('gd-phone-hide')) return;
          const copy = el.cloneNode(true);
          // .btn-sm is the size for a row of controls inside a card; .mobile-actbar's own
          // rule gives its buttons full size, which is what a phone's primary surface
          // wants (DESIGN.md 3.5).
          copy.classList.remove('btn-sm');
          bar.appendChild(copy);
        });
      }
      const more = document.createElement('button');
      more.className = 'btn btn-icon';
      more.dataset.act = 'overflow';
      more.setAttribute('aria-label', 'More actions');
      more.innerHTML = '&#8943;';
      bar.appendChild(more);
    }
    syncBar();
  }

  // Lifted from static/js/account-detail.js, including both of its fallbacks: with no
  // inline bar and with no IntersectionObserver the sticky bar SHOWS, because a fallback
  // that hid it could strand the page's primary action off screen with no way to reach it.
  //
  // The one addition is the `selecting` clause: in selection mode the bar carries verbs
  // that exist nowhere else on the page, so it must not hide itself just because the user
  // happens to be scrolled to the top.
  let inlineOffScreen = false;
  function setBar(on) {
    const bar = byId('gd-bottombar');
    if (!bar) return;
    bar.classList.toggle('on', on);
    bar.setAttribute('aria-hidden', on ? 'false' : 'true');
    document.body.classList.toggle('actbar-on', on);
  }
  function syncBar() { setBar(selecting || inlineOffScreen); }

  function initBarReveal() {
    const inline = byId('gd-inline-actionbar');
    if (!inline || typeof IntersectionObserver === 'undefined') {
      inlineOffScreen = true;
      syncBar();
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => { inlineOffScreen = !entry.isIntersecting; });
      syncBar();
    }, { threshold: 0 });
    observer.observe(inline);
    // A live observer toggling a bar that belongs to a page you have left is how a fixed
    // control ends up floating over the wrong page.
    window.addEventListener('pagehide', () => observer.disconnect());
  }

  // ── Summary + settings bar ────────────────────────────────────────────────

  function renderSummary() {
    const track = byId('gd-sum-track');
    if (!track) return;
    // The bar shows ONE run, so once the group carries a second check the label has to say
    // whose run it is - otherwise the number silently belongs to whichever sorted first.
    const lbl = byId('gd-sum-lbl-run');
    if (lbl && (G.checks || []).length > 1) {
      lbl.innerHTML = `Last run results &#183; ${escHtml(G.jobName || '')}`;
    }
    const c = COUNTS;
    const untested = Math.max(0, TOTAL - c.tested_count);
    const seg = (kind, n) => (n ? `<div class="gd-sum-seg ${kind}" style="flex:${n}"></div>` : '');
    const segs = seg('pass', c.pass_count) + seg('warn', c.warn_count) + seg('fail', c.fail_count) + seg('untested', untested);
    const pct = (n) => (TOTAL ? Math.round(n / TOTAL * 100) : 0);
    track.innerHTML = segs || '<div class="gd-sum-seg untested" style="flex:1"></div>';
    track.className = 'gd-sum-track tip-plain';
    track.setAttribute('data-tip',
      `Last run results.&#10;The most recent run of "${G.jobName || 'this check'}": ` +
      `${c.tested_count} of ${TOTAL} tested - ${c.pass_count} passed, ${c.warn_count} warned, ${c.fail_count} failed` +
      (untested ? `, ${untested} not tested` : '') + '.');
    byId('gd-sum-legend').innerHTML =
      `<span class="gd-sl"><i class="gd-sl-dot pass"></i>Passed <strong>${c.pass_count}</strong> (${pct(c.pass_count)}%)</span>` +
      `<span class="gd-sl"><i class="gd-sl-dot warn"></i>Warned <strong>${c.warn_count}</strong> (${pct(c.warn_count)}%)</span>` +
      `<span class="gd-sl"><i class="gd-sl-dot fail"></i>Failed <strong>${c.fail_count}</strong> (${pct(c.fail_count)}%)</span>` +
      `<span class="gd-sl text-muted">Tested <strong>${c.tested_count} / ${TOTAL}</strong></span>`;
  }

  function settingItems() {
    const items = [];
    if (G.hasChannel) {
      const strategy = strategyValue();
      items.push({
        focus: 'format', label: 'Format strategy', value: groupStrategyLabel(strategy),
        tip: `${groupStrategyHelp(strategy)} Format lock filters, health score ranks.`,
      });
      // The second chip has to say WHERE the format came from, not just what it is: a
      // format the strategy chose overnight and a format the user pinned look identical
      // otherwise, and only one of them will still be there tomorrow. It renders for any
      // recording source, including one enforcing nothing - "not enforced" is a value the
      // user needs to see, not an absence - and the stored lock is deliberately hidden
      // when nothing is enforcing it.
      const shown = groupStrategyManagesFormat(strategy)
        ? (G.lockLabel || G.referenceLabel) : null;
      if (shown || strategy === 'unmanaged') {
        let origin, tip;
        if (strategy === 'manual') {
          origin = 'pinned by you';
          tip = 'You pinned this format. Nothing moves it - not a health check run, not a provider change.';
        } else if (strategy === 'unmanaged') {
          origin = 'not enforced';
          tip = 'No format management. Nothing is filtered, so a recording may fail over between ' +
            'two different formats.';
        } else if (G.lockLabel) {
          origin = `selected automatically by ${groupStrategyLabel(strategy)}`;
          tip = `Chosen automatically by the "${groupStrategyLabel(strategy)}" strategy, and ` +
            're-chosen after every health check run - so it can move on its own. The group ' +
            'timeline records each time it does.';
        } else {
          origin = 'follows the healthiest member';
          tip = 'Not locked. Taken from the highest-scoring member with a known format, so it ' +
            'moves when that member does. Nothing is filtered while there is no lock - members ' +
            'reporting another format stay selectable, but a recording keeps the format it ' +
            'starts on, so it would not fail over to them.';
        }
        // How many willing members actually share that format, next to the format itself.
        // The rows dim one at a time and a group this size is scrolled, so without the
        // count the page states a format and leaves the reader to tally who is under it.
        // Server-counted against the same reference the dimming reads, so the tally and
        // the dimmed rows cannot disagree. Withheld when nothing is enforcing a format at
        // all (`unmanaged` shows "Any"), where there is no denominator to count against.
        const matched = WARN && WARN.format_match_count;
        const sub = (shown && matched !== null && matched !== undefined && WARN.recording_count)
          ? `${origin} · ${matched} of ${WARN.recording_count} match`
          : origin;
        items.push({
          focus: 'format', label: 'Group format', value: shown || 'Any', sub, tip,
        });
      }
    }
    const checks = G.checks || [];
    if (G.hasCheck) {
      // Schedule and profile belong to a CHECK, not to the group, so a group carrying more
      // than one gets a pair of chips per check with the check's name in the label. A chip
      // for a check this page is not pinned to links to that check's own page rather than
      // opening the Settings modal, which would edit the pinned job instead.
      const multi = checks.length > 1;
      const scoped = multi ? checks : [null];
      scoped.forEach(c => {
        const suffix = c ? ` · ${c.name}` : '';
        const href = (c && !c.is_primary) ? c.detail_url : null;
        // Never conditional on the schedule's value: a check with no schedule still gets
        // a chip, it just reads "None" rather than disappearing.
        items.push({
          focus: 'check', label: `Schedule${suffix}`, href,
          value: (c ? c.schedule_label : (G.schedule && G.schedule.label)) || 'None',
          tip: 'When this health check re-tests every channel: nothing automatic, a single run at a set time, or a recurring day and time. Times use your configured timezone.',
        });
        items.push({
          focus: 'check', label: `Test profile${suffix}`, href,
          value: (c ? c.profile_name : G.profileName) || 'Default settings',
          tip: 'How long each channel is watched and how strict the pass thresholds are. Profiles are edited under Health Check Profiles.',
        });
      });
    }
    return items;
  }

  function renderSettingsBar() {
    const host = byId('gd-setbar');
    // The desktop's wrapping row of pills does not survive 375px: it wraps into an uneven
    // block, and the one pill carrying an origin line comes out double height beside its
    // neighbors. Same four facts, same click target, as the app's key/value list
    // (.statlist, DESIGN.md 3.3) - which is what an account page's Details card already is
    // and which collapses to one column at 768 on its own. Each row stays a button opening
    // Settings focused on its key, exactly as the pill did; the chevron is what says so.
    if (isPhone()) {
      host.className = '';
      host.innerHTML = '<div class="statlist">' + settingItems().map(it => {
        const value = `<span>${escHtml(it.value)}` +
          (it.sub ? `<span class="srow-sub">${escHtml(it.sub)}</span>` : '') + '</span>';
        const inner = `<span class="sk">${escHtml(it.label)}</span><span class="sv">${value}` +
          '<span class="srow-go" aria-hidden="true">&rsaquo;</span></span>';
        const tip = `${escHtml(it.label)}.&#10;${escHtml(it.tip)} ` +
                    (it.href ? 'Tap to open that health check.' : 'Tap to change it.');
        return it.href
          ? `<a class="srow srow-act tip-plain" href="${escHtml(it.href)}" data-tip="${tip}">${inner}</a>`
          : `<button type="button" class="srow srow-act tip-plain" data-act="settings"` +
            ` data-focus="${it.focus}" data-tip="${tip}">${inner}</button>`;
      }).join('') + '</div>';
      return;
    }
    host.className = 'gd-setbar';
    host.innerHTML = settingItems().map(it => {
      const inner = `<span class="gd-chip-lbl">${escHtml(it.label)}</span>` +
                    `<span class="gd-chip-val">${escHtml(it.value)}</span>` +
                    (it.sub ? `<span class="gd-sub">${escHtml(it.sub)}</span>` : '');
      const tip = `${escHtml(it.label)}.&#10;${escHtml(it.tip)} ` +
                  (it.href ? 'Click to open that health check.' : 'Click to change it.');
      return it.href
        ? `<a class="gd-chip tip-plain" href="${escHtml(it.href)}" data-tip="${tip}">${inner}</a>`
        : `<button type="button" class="gd-chip tip-plain" data-act="settings" data-focus="${it.focus}"` +
          ` data-tip="${tip}">${inner}</button>`;
    }).join('');
  }

  // ── Warning banners (DESIGN-channel-groups-model.md §16) ──────────────────
  // Sole updater for the five banner regions. Since nothing is auto-corrected any more
  // (§4.1) and almost nothing is refused (§4.3, §15), these ARE what the app does about
  // a questionable setup - which makes the copy load bearing rather than decorative.
  //
  // Every one of them is gated on the group being a recording source, i.e. on the
  // strategy, never on `in_guide`. That is one condition instead of two and it is the
  // right one: a group being prepared for the guide should be warned BEFORE it gets
  // there, and a group made purely for health checking should not be warned at all.

  // The three mutable banners, as the Settings > Warnings block lists them. Keyed on
  // app/database.py::GROUP_WARNING_KINDS, which the route validates against - this list
  // is the copy, never the authority.
  const WARN_SETTINGS = [
    ['epg', 'Warn about mismatched EPG data',
     'This group fills one guide row from whichever member wins each program, so members ' +
     'carrying unrelated listings paint a row that looks right and is wrong. Turn this off ' +
     'for a group whose members you know are the same channel despite different EPG ids.'],
    ['format', 'Warn about mixed video formats',
     'Only for a format you pinned by hand: members with Recording on that report a ' +
     'different resolution or frame rate are skipped when a recording picks a member. An ' +
     'automatic strategy never shows this warning, because moving between formats is its job.'],
    ['override', 'Warn when no member matches the group format',
     'ChannelBin records rather than skips when every recording-enabled member is filtered ' +
     'out, so the file exists but is not the format you asked for. Hiding this does not ' +
     'affect the alerts or what the recording itself records.'],
  ];

  // The hide control every mutable banner carries. One function so three banners cannot
  // drift apart in wording or behavior.
  function muteBtn(kind, why) {
    const tip = `Hide this warning for this group.&#10;${escHtml(why)} Turn it back on ` +
      'any time under Settings &gt; Warnings.';
    return `<button type="button" class="btn btn-sm tip-plain" data-act="mute" ` +
      `data-mute="${kind}" data-tip="${tip}">Hide this warning</button>`;
  }

  // What the banners are for, rendered once above the stack
  // into #gd-banner-explainer (which already carries the gd-sub/gd-bannote styling)
  // rather than repeated inside each mutable banner.
  function bannerNote() {
    return 'These warnings flag an invalid recording configuration. They clear on their ' +
      'own once the group is set up cleanly. You can hide any of them if you would ' +
      'rather not see it.';
  }

  function strategyBtn(label) {
    return `<button type="button" class="btn btn-sm" data-act="settings" data-focus="format">` +
      `${escHtml(label)}</button>`;
  }

  function renderBanners() {
    if (!WARN) return;
    const muted = new Set(WARN.muted || []);
    const strategy = strategyValue();
    const label = groupStrategyLabel(strategy);
    const fmt = G.lockLabel || G.referenceLabel || 'unknown';
    const set = (id, html) => {
      const el = byId(id);
      if (!el) return;
      el.style.display = html ? '' : 'none';
      if (html) el.innerHTML = html;
    };

    // Both format banners fire only for a format pinned by hand with a recording-enabled
    // member off it (`format_warns`, decided server-side, dev/changelog/925). Under an
    // automatic strategy or `unmanaged`, members spanning formats is expected and moving
    // between them is the point, so neither banner has anything true to say there.
    let mixed = '';
    const n = WARN.format_blocked_count;
    if (WARN.format_warns && n && !muted.has('format')) {
      mixed = `<strong>&#9940; Mixed video format in this group</strong><br>` +
        `Group format is pinned by hand to <strong>${escHtml(fmt)}</strong>, and ` +
        `${plural(n, 'member')} with Recording on report a different resolution or frame rate. ` +
        `${n === 1 ? 'It is' : 'They are'} still switched on for recording, and ` +
        `${n === 1 ? 'is' : 'are'} skipped when a recording picks a member - nothing has been ` +
        `turned off, and ${n === 1 ? 'it becomes' : 'they become'} eligible again on ` +
        `${n === 1 ? 'its' : 'their'} own once the formats match.` +
        `<div class="gd-ban-acts">${strategyBtn('Change the format strategy')} ` +
        '<button type="button" class="btn btn-sm" data-act="review-members" data-review="format">' +
        'Review members</button> ' +
        muteBtn('format', 'For when you already know these members differ.') + '</div>';
    }
    set('gd-format-banner', mixed);

    // §15.2: every willing member filtered out. The recording is NOT skipped - it runs
    // from the best-ranked enabled member and says so. This banner is one of the two
    // voices; the other (the recording's own RECORDING_FORMAT_OVERRIDE event) is
    // unaffected by hiding it, which its tooltip says out loud.
    let override = '';
    if (WARN.format_warns && WARN.format_override && !muted.has('override')) {
      const who = WARN.override_member_name || 'the highest-ranked member';
      const at = WARN.override_member_format
        ? ` at ${escHtml(WARN.override_member_format)}` : '';
      override = `<strong>&#9940; No member matches the group format</strong><br>` +
        `All ${plural(WARN.recording_count, 'recording-enabled member')} report a format other ` +
        `than <strong>${escHtml(fmt)}</strong>. ChannelBin will still record rather than skip: ` +
        `the next recording runs from <strong>${escHtml(who)}</strong>${at}, and the recording ` +
        'itself will say so on its detail page.' +
        `<div class="gd-ban-acts">${strategyBtn('Change the format strategy')} ` +
        muteBtn('override', "The recording's own record is unaffected by hiding this.") +
        '</div>';
    }
    set('gd-override-banner', override);

    // EPG mismatch (§8). Warn, never block - and only for a group already in the guide,
    // because that is the only state in which two schedules would actually paint one row.
    let epg = '';
    const ids = WARN.epg_ids || [];
    if (G.inGuide && ids.length > 1 && !muted.has('epg')) {
      const shown = ids.slice(0, 6);
      epg = '<strong>&#9888; These members carry different EPG data</strong><br>' +
        'This group fills one guide row per program from whichever member wins, so ' +
        `${ids.length} different program schedules would paint one row. The listings would ` +
        'look plausible and be wrong.' +
        // Each id is its own action, which is what makes the tally answerable rather than
        // only countable: the button below shows every id at once, and this shows the one
        // you are looking at. Same inline `<a data-act>` the unmonitored banner uses.
        '<ul class="gd-epg-list">' + shown.map(e =>
          `<li><a href="#" data-act="review-epg" data-epg="${escHtml(e.epg_channel_id)}">` +
          `${escHtml(e.epg_channel_id)}</a> - ${plural(e.count, 'member')}</li>`).join('') +
        (ids.length > 6 ? `<li>and ${ids.length - 6} more</li>` : '') + '</ul>' +
        (WARN.epg_missing_count
          ? `<div class="gd-sub"><a href="#" data-act="review-epg" data-epg="">` +
            `${plural(WARN.epg_missing_count, 'member')}</a> ` +
            `${WARN.epg_missing_count === 1 ? 'has' : 'have'} no EPG id at all, which is ` +
            'unknown rather than mismatched.</div>'
          : '') +
        '<div class="gd-ban-acts">' +
        '<button type="button" class="btn btn-sm" data-act="review-members" data-review="epg">' +
        'Review members</button> ' +
        muteBtn('epg', 'For when you know these feeds are the same channel even though their EPG ids differ.') +
        '</div>';
    }
    set('gd-epg-banner', epg);
    // Shown once above the whole stack whenever any of the three banners above needs it,
    // rather than repeated inside each one - DESIGN-channel-groups-model.md:1108.
    set('gd-banner-explainer', (mixed || override || epg) ? bannerNote() : '');

    // The no-winner state, shown while it is TRUE rather than only in the one
    // GROUP_FORMAT_STRATEGY_BLOCKED event written on the way into it. Right after a
    // database wipe every group is here, so a banner that appeared only at the
    // transition would be missing for exactly the people who need it. Not mutable:
    // nobody chose this state, and it is the reason the group is not doing what its
    // settings say it does.
    let noWinner = '';
    if (WARN.no_winner) {
      noWinner = '<div class="notice-banner-body"><span class="notice-banner-title">' +
        `"${escHtml(label)}" could not choose a format.</span><div class="notice-banner-sub">` +
        `${escHtml(WARN.no_winner_rationale || '')} Nothing is filtered while that is true, so ` +
        'the group records from whichever member ranks highest.</div></div>';
    }
    const nw = byId('gd-nowinner-banner');
    if (nw) {
      nw.className = 'notice-banner notice-banner-warn';
      nw.style.display = noWinner ? '' : 'none';
      if (noWinner) nw.innerHTML = noWinner;
    }

    // §15's invariant, breached. Only reachable down the paths that cannot ask - a bulk
    // delete of channels the provider dropped, or a dedup transfer on another group - so
    // the row is deliberately still here and this says why rather than pretending
    // otherwise. Not mutable: this is the explanation for why the group cannot do what
    // its settings say, which §16.2 keeps out of the hide-able set.
    let broken = '';
    if (WARN.guide_broken) {
      broken = '<strong>&#9940; In the TV Guide with nothing to record from</strong><br>' +
        'No member of this group is switched on for recording, so its guide row cannot ' +
        'produce a file. The row is still here on purpose - pulling it without asking is ' +
        'not something ChannelBin does - but a recording started from it now has no ' +
        'member to use.' +
        '<div class="gd-ban-acts">' +
        '<button type="button" class="btn btn-sm btn-primary" data-act="walkthrough">Set up recording</button> ' +
        '<button type="button" class="btn btn-sm" data-act="guide-toggle">Remove from the TV Guide</button>' +
        '</div>';
    }
    set('gd-broken-banner', broken);

    // Drift coverage. Rendered here rather than by the template so it follows the Health
    // check switches, which move without a reload (dev/changelog/757).
    let unmon = '';
    const un = WARN.unmonitored_count || 0;
    if (un) {
      unmon = `<strong>&#9888; ${plural(un, 'channel')} not monitored</strong><br>` +
        `No active recurring health check covers ${un === 1 ? 'it' : 'them'}, so a ` +
        'provider-side resolution or frame-rate change will not be re-checked automatically.' +
        (WARN.has_check ? ''
          : ' <a href="#" data-act="create-check">Schedule a health check for this group</a>.');
    }
    set('gd-unmonitored-banner', unmon);
  }

  // §16.2's "fix" half: every banner that names a problem with the members has to be able to
  // put them in front of you. The mockup left this as a placeholder toast because a mockup has
  // no member list to filter; here it filters the real one to the recording-enabled members -
  // exactly the set every one of these banners is counting - and scrolls to it. A button that
  // only explained itself would be the dead knob §16.1 refuses one control down.
  //
  // The target says WHICH set, because the button is shared and the banners are not asking
  // the same question. Everything §16 counts is recording-enabled, so that is the floor on
  // every target; the EPG banner adds the ids it named, and turns on the column that shows
  // them - a filter chip reading "EPG id: x" over a table with no such column names the
  // answer without showing it (dev/changelog/898).
  function reviewMembers(target, epgId) {
    // Replaces whatever was filtered rather than adding to it: the banner is naming one
    // specific set, so the list has to end up showing that set and not an intersection
    // with something left on from earlier. The chip it leaves behind is what says so.
    filterBar.clear();
    filterBar.toggle('rec', 'on');
    let revealed = false;
    if (target === 'epg') {
      // With no id named: every id the banner listed, never a subset. Its list is ordered
      // by count, so "all but the most common" would read as the mismatch - but it would
      // also hide members the banner just counted, and a filter that quietly drops rows the
      // warning included is the thing this button exists to stop doing. Narrowing from here
      // is a chip away, and each id in the banner's list is its own link for going straight
      // to one. `epgId` is the empty string for the no-id bucket, which is a real answer and
      // not the absent one.
      const vals = epgId === undefined
        ? epgValues().filter(o => o.v !== EPG_NONE).map(o => o.v)
        : [epgId ? epgKey(epgId) : EPG_NONE];
      vals.forEach(v => filterBar.toggle('epg', v));
      if (!fieldOn('epg')) { toggleField('epg', true); revealed = true; }
    }
    searchTerm = '';
    const box = byId('gd-search');
    if (box) {
      box.value = '';
      byId('gd-search-wrap').classList.remove('has-text');
    }
    // apply() redraws the chips, the phone's sheet and the list from the one state. The
    // Columns popover is not on that path - it is built once and rebuilt on demand - so a
    // field revealed here has to put its own checkbox back in agreement.
    filterBar.apply();
    if (revealed) {
      buildColMenu();
      // Turning a column on is a stored preference this button changed on the user's
      // behalf, and a display setting that moves with nothing said is the silence this app
      // exists to refuse. It stays on afterwards, like any other column: the Columns menu
      // is where it goes back off.
      showToast('Filtered the member list, and turned on the EPG id column so you can see ' +
        'which member carries which. Turn it back off under Columns.');
    }
    const card = document.querySelector('[data-section="channels"]');
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function muteWarning(kind) {
    jsonFetch(api('warnings'), {
      method: 'POST',
      body: JSON.stringify({ warnings: { [kind]: false } }),
    }).then(data => {
      WARN.muted = data.muted;
      renderBanners();
      showToast('Hidden for this group. Turn it back on under Settings > Warnings - nothing ' +
        'about the group itself has changed.');
    }).catch(err => showToast(err.message || 'Could not hide that warning.', { type: 'error' }));
  }

  // ── Duplicates ────────────────────────────────────────────────────────────
  // Sole updater for #gd-dup-banner. A duplicate set only costs anything while 2+ of
  // its members are still enabled, so disabling the extras drops it to the quiet
  // variant rather than nagging on.

  function renderDupBanner() {
    const el = byId('gd-dup-banner');
    const kebab = byId('gd-kebab-dedup');
    if (kebab) kebab.disabled = G.isSystem || !DUP_GROUPS.length;
    if (!DUP_GROUPS.length) { el.style.display = 'none'; return; }

    const sets = DUP_GROUPS.length;
    const activeSets = DUP_GROUPS.filter(g => (g.channels || []).filter(c => !c.disabled).length > 1).length;
    const redundant = DUP_GROUPS.reduce((n, g) => n + (g.channels || []).length, 0) - sets;
    const where = G.hasChannel ? 'in this group' : 'in this health check';

    if (!activeSets) {
      const why = G.hasChannel && G.hasCheck ? 'nothing is tested twice and failover never lands on them'
        : G.hasChannel ? 'failover never lands on them' : 'nothing is tested twice';
      el.className = 'notice-banner notice-banner-muted';
      el.innerHTML = `<div class="notice-banner-body"><span class="notice-banner-title">` +
        `${plural(sets, 'duplicate channel set')} ${where} - the extras are disabled, so ${why}.</span></div>`;
      el.style.display = '';
      return;
    }

    const cost = G.hasChannel && G.hasCheck
      ? 'every run tests the same feed more than once, and failover can drop off a failing feed straight onto the very same stream.'
      : G.hasChannel
        ? 'failover can drop off a failing feed straight onto the very same stream instead of a genuinely different one, so one outage takes the whole group down with it.'
        : 'each run tests it more than once and burns an extra connection on your account.';
    // An automatic group computes its own membership, so there is nothing to remove
    // here - the fix is in Browse, where the channels themselves live.
    const action = G.isSystem
      ? `<a class="btn btn-sm" href="${escHtml(G.browseUrl)}">Manage in Browse</a>`
      : `<button type="button" class="btn btn-sm btn-danger-outline" data-act="dedup">Review duplicates&hellip;</button>`;
    el.className = 'notice-banner notice-banner-warn';
    el.innerHTML =
      `<div class="notice-banner-body">` +
      `<div class="notice-banner-title">&#9888; ${plural(activeSets, 'duplicate channel set')} ${where}</div>` +
      `<div class="notice-banner-sub">${plural(redundant, 'channel')} here ` +
      `${redundant === 1 ? 'points' : 'point'} at a stream another channel already covers, so ${cost} ` +
      `Keep one from each set, or disable the extras.</div></div>${action}`;
    el.style.display = '';
  }

  // What "remove" means differs per page state, and dup-modal.js makes every caller
  // spell it out for exactly that reason.
  // The dedup submit, named rather than inline so §15's confirm can re-run the identical
  // removal list. Removing duplicates empties the recording-enabled set as surely as
  // unticking them does, so it takes the same gate - and the retry must resubmit exactly
  // what the user reviewed, not a set recomputed from a table that has since refreshed.
  function submitDedup(removals, transfer, confirmed) {
    const req = G.hasChannel
      ? jsonFetch(api('members/remove'), {
          method: 'POST', body: JSON.stringify({ removals, transfer, confirm: !!confirmed }),
        })
      : jsonFetch(jobApi('remove-duplicates'), {
          method: 'POST', body: JSON.stringify({ removals, transfer }),
        });
    return req.then((resp) => {
      refreshRows();
      if (resp && resp.left_guide) {
        showToast(`${G.groupName} has left the TV Guide - the duplicates you removed were ` +
          'the only members switched on for recording' +
          (resp.cancelled_recordings
            ? `, and ${plural(resp.cancelled_recordings, 'scheduled recording')} was cancelled.`
            : '.'), { type: 'warning' });
      }
      return { ok: true };
    }).catch(e => {
      if (handleInvariantError(e, () => submitDedup(removals, transfer, true), () => {})) {
        // Handled: the dialog now owns the outcome, so the modal closes rather than
        // showing an error beside a question the user is already being asked.
        return { ok: true };
      }
      return { error: e.message || 'Request failed.' };
    });
  }

  function openDedup() {
    if (!DUP_GROUPS.length) { showToast('No duplicate channels here - nothing to review.', { type: 'error' }); return; }
    const intro = G.hasChannel
      ? 'The channels you don\'t keep are removed from <strong>this group</strong>' +
        (G.hasCheck ? ', which also takes them out of the health check and its test history here' : '') +
        '. They are not deleted &mdash; they stay in your channel list and return to the guide as ' +
        'individual channels if they were in it before.'
      : 'The channels you don\'t keep are removed from <strong>this health check</strong> only, along ' +
        'with their test history here. They are not deleted &mdash; they stay in your channel list and ' +
        'in the TV Guide unless you choose otherwise below.';
    openDupModal({
      groups: DUP_GROUPS,
      showGuideChoice: !G.hasChannel,
      introHtml: intro,
      onSubmit: (removals, transfer) => submitDedup(removals, transfer, false),
    });
  }

  // ── Missing channels ────────────────────────────────────────────────────────
  // Sole updater for #gd-missing-banner. A missing channel is one the provider has
  // stopped sending in a sync for a while (channel_lifecycle_state() 'missing') -
  // scoped delete of the same mechanism channel search's own "Delete Missing
  // Channels" uses, via the shared static/js/missing-modal.js (dev/changelog/653).

  function renderMissingBanner() {
    const el = byId('gd-missing-banner');
    const kebab = byId('gd-kebab-missing');
    if (kebab) kebab.disabled = !MISSING_CHANNELS.length;
    if (!MISSING_CHANNELS.length) { el.style.display = 'none'; return; }

    const where = G.hasChannel ? 'in this group' : 'in this health check';
    el.className = 'notice-banner notice-banner-warn';
    el.innerHTML =
      `<div class="notice-banner-body">` +
      `<div class="notice-banner-title">&#9888; ${plural(MISSING_CHANNELS.length, 'channel')} ${where} ` +
      `no longer seen in a provider sync</div>` +
      `<div class="notice-banner-sub">Still listed here, but the provider has stopped sending ` +
      `${MISSING_CHANNELS.length === 1 ? 'it' : 'them'} for a while.</div></div>` +
      `<button type="button" class="btn btn-sm btn-danger-outline" data-act="delete-missing">Delete missing channels&hellip;</button>`;
    el.style.display = '';
  }

  function openDeleteMissing() {
    if (!MISSING_CHANNELS.length) { showToast('No missing channels here.', { type: 'error' }); return; }
    openMissingModal({
      title: 'Delete Missing Channels',
      scopeText: G.hasChannel ? 'in this group' : 'in this health check',
      previewUrl: `${G.missingPreviewUrl}?group_id=${G.groupId}`,
      deleteUrl: G.missingDeleteUrl,
      deleteBody: { group_id: G.groupId },
      onDone: () => refreshRows(),
    });
  }

  // The phone bulk sheet's scoped counterpart to openDeleteMissing() above - the
  // selection narrows WHICH of this group's channels are considered, exactly as
  // static/js/channel-search.js's own selection-delete does against the same shared
  // modal (dev/changelog/772). group_id is sent alongside channel_ids, not instead of
  // it: without it, being a member of THIS group would itself count as "in a group" and
  // block every row, since only group_id carries the exemption for the group the request
  // is already scoped to. The server intersects the two rather than letting either win.
  function openDeleteMissingSelected() {
    const ids = selectedMissingIds();
    if (!ids.length) { showToast('None of the selected channels are missing.', { type: 'error' }); return; }
    openMissingModal({
      title: 'Delete Missing Channels',
      scopeText: 'from your selection',
      emptyText: 'None of the selected channels can be deleted right now.',
      previewUrl: `${G.missingPreviewUrl}?group_id=${G.groupId}&channel_ids=${ids.join(',')}`,
      deleteUrl: G.missingDeleteUrl,
      deleteBody: { group_id: G.groupId, channel_ids: ids },
      onDone: () => { setSelecting(false); refreshRows(); },
    });
  }

  // ── Settings modal ────────────────────────────────────────────────────────
  // One modal behind every settings chip, laid out like the real Settings page
  // (label + description on the left, control on the right). Each block commits
  // through its own endpoint, so a Save never bundles two unrelated writes.

  // fieldRow() lives in util.js - see the note there. Do not re-declare it here.

  // ── The Format block ──────────────────────────────────────────────────────
  // Redrawn on every strategy change (the help text, the Pinned format control and the
  // bucket table's winner marker all move with it), so it lives in its own container -
  // the Health check fieldset below hosts the mounted schedule fields and must never be
  // rebuilt underneath them.
  //
  // The bucket table comes from the server (GET /format-plan) through format-plan.js.
  // The approved mockup reimplemented the whole bucket engine in JS because a mockup has
  // no server; the shipped page must not, or the table and the lock can disagree about
  // which format won.

  // The live pick, so Save can tell a real change from a no-op and the redraw can render
  // what the user chose rather than what is stored.
  const fmtEdit = { strategy: null, pin: null, plan: null, planState: 'loading' };

  // How many members have a measured format at all. The format summary needs it to tell
  // "measures a different format, so it is skipped" apart from "never tested, so it is
  // still eligible" - the two behave differently and one number covering both was wrong
  // in the direction that matters (dev/changelog/757).
  //
  // Taken from the plan, not from ROWS: the rows are scoped to the attached health check
  // while the lock is decided from each member's own latest test, so counting them here
  // answered a different question than the sentence it appears in (dev/changelog/890).
  function measuredMemberCount() {
    return fmtEdit.plan ? fmtEdit.plan.rank_measured : null;
  }

  function drawFormatBlock(host, focus) {
    const strategy = fmtEdit.strategy;
    const manages = groupStrategyManagesFormat(strategy);
    // The format the group's DATA points at, never its lock. `G.lockLabel ||
    // G.referenceLabel` was used here and labelled "Healthiest member's format" with the
    // group's existing lock - on a group locked to 3840x2160 @ 50 it offered that back
    // while the healthiest member measured 1920x1080 @ 50 (dev/changelog/890).
    const derived = G.derivedReferenceLabel || null;
    const plan = fmtEdit.plan;
    let h = `<fieldset class="gd-fset${focus === 'format' ? ' hi' : ''}">` +
      '<div class="gd-fset-head">Format</div>';

    const opts = fmtEdit.planState === 'loading'
      ? '<option>Loading&hellip;</option>'
      : GROUP_FORMAT_STRATEGIES.map(([key]) =>
        `<option value="${key}"${key === strategy ? ' selected' : ''}>` +
        `${escHtml(formatPlanOptionLabel(plan, key, derived))}</option>`).join('');
    h += fieldRow({
      wide: true,
      label: 'Format strategy',
      meta: 'Which video format this group should be, and whether it is a recording source ' +
        'at all. Unlike picking a format once, this is re-evaluated after every health check ' +
        'run, so the format follows the data.' +
        // `.none` colors the sentence as a warning for the two values that enforce
        // nothing - it is the one thing about this control a user can get wrong quietly.
        `<div class="gd-strategy-help${manages ? '' : ' none'}">` +
        `${escHtml(groupStrategyHelp(strategy))}</div>`,
      control: `<select id="gd-strategy"${fmtEdit.planState === 'loading' ? ' disabled' : ''}>` +
        `${opts}</select>`,
    });

    // `manual` is the one value that needs a second control. Without it the strategy
    // could be set to "Pinned format" with no way to say to what.
    if (strategy === 'manual') {
      h += fieldRow({
        wide: true,
        label: 'Pinned format',
        meta: 'The format this group is locked to. Nothing moves it - not a health check ' +
          'run, not a provider change.',
        control: formatPlanPinSelect('gd-pin', plan, fmtEdit.pin),
      });
    }

    h += fieldRow({
      full: true,
      label: '',
      meta: escHtml(formatPlanSummary(plan, strategy, fmtEdit.pin, derived, TOTAL,
                                      measuredMemberCount())) +
        (manages && fmtEdit.planState === 'ready'
          ? `<div class="cg-auto-details">${formatPlanTable(plan, strategy, fmtEdit.pin, derived)}</div>`
          : '') +
        (fmtEdit.planState === 'error'
          ? '<p class="text-muted">The measured formats could not be loaded, so the table ' +
            'below is unavailable. The strategy itself still saves.</p>'
          : ''),
    });

    // The sentence that summarizes the whole three-layer story, kept where the choice is
    // made rather than only in the design doc.
    h += fieldRow({
      full: true,
      label: '',
      meta: '<strong>Format lock filters, health score ranks.</strong> The lock protects you ' +
        'within one recording; the strategy chooses between recordings.',
    });

    if (manages) {
      h += '</fieldset><fieldset class="gd-fset">' +
        '<div class="gd-fset-head">Members that do not match</div>';
      h += fieldRow({
        full: true,
        label: '',
        meta: 'Nothing is switched off for you. A member whose format does not match is ' +
          'skipped when a recording picks a member, keeps its switches exactly as you set ' +
          'them, and becomes eligible again by itself if its format changes back.',
      });
    }
    host.innerHTML = h + '</fieldset>';
  }

  function openSettingsModal(focus) {
    const body = document.createElement('div');
    let h = '';

    if (G.hasChannel) {
      h += '<div id="gd-fmt-block"></div>';
    }

    if (G.hasCheck) {
      const s = G.schedule || { mode: 'manual' };
      h += `<fieldset class="gd-fset${focus === 'check' ? ' hi' : ''}"><div class="gd-fset-head">Health check</div>`;
      h += fieldRow({
        label: 'Schedule',
        meta: '<p><strong>None</strong> means no automatic schedule - run it yourself from Test again. ' +
          '<strong>One time</strong> runs once at a date and time you pick. <strong>Recurring</strong> ' +
          'repeats on a day and time.</p><p>Runs are queued, so a check never competes with a recording ' +
          'for the same feed.</p>',
        control: `<select id="gd-sched-mode">` +
          `<option value="manual"${s.mode === 'manual' ? ' selected' : ''}>None</option>` +
          `<option value="once"${s.mode === 'once' ? ' selected' : ''}>One time</option>` +
          `<option value="recur"${s.mode === 'recur' ? ' selected' : ''}>Recurring</option></select>`,
      });
      h += `<div class="gd-field sub" id="gd-sched-fields"></div>`;
      h += fieldRow({
        label: 'Test profile',
        meta: 'How long each channel is watched and how strict the pass thresholds are. Longer profiles ' +
          'catch stalls a short one misses, at the cost of a longer run. Profiles themselves are edited ' +
          `under <a href="${escHtml(G.profilesUrl)}" target="_blank" rel="noopener">Health Check Profiles</a>.`,
        control: `<select id="gd-profile"><option value="">Default settings</option>` +
          G.profiles.map(p => `<option value="${p.id}"${p.id === G.profileId ? ' selected' : ''}>${escHtml(p.name)}</option>`).join('') +
          '</select>',
      });
      h += '</fieldset>';
    }

    // The re-entry point for every warning the user dismissed. Without one, "Hide this
    // warning" is a one-way door - and there are three of them, so this block is built
    // from a list rather than written out three times.
    if (G.hasChannel && WARN) {
      const muted = new Set(WARN.muted || []);
      h += '<fieldset class="gd-fset"><div class="gd-fset-head">Warnings</div>';
      h += WARN_SETTINGS.map(([kind, label, meta]) => fieldRow({
        label: escHtml(label),
        meta: escHtml(meta) + (muted.has(kind)
          ? '<p class="text-muted">Currently off - you hid this warning for this group.</p>' : ''),
        control: `<label class="switch"><input type="checkbox" data-warn="${kind}"` +
          `${muted.has(kind) ? '' : ' checked'}><span class="knob"></span></label>`,
      })).join('');
      h += '</fieldset>';
    }

    body.innerHTML = h;

    const fmtHost = body.querySelector('#gd-fmt-block');
    if (fmtHost) {
      fmtEdit.strategy = strategyValue();
      fmtEdit.pin = G.lockLabel || G.referenceLabel || null;
      fmtEdit.plan = null;
      fmtEdit.planState = 'loading';
      drawFormatBlock(fmtHost, focus);
      fetchFormatPlan(G.groupId)
        .then(plan => { fmtEdit.plan = plan; fmtEdit.planState = 'ready'; })
        .catch(() => { fmtEdit.planState = 'error'; })
        // Redrawn either way: leaving the picker stuck on "Loading..." because the plan
        // failed would make the whole setting unreachable over a table that is only ever
        // an explanation of it.
        .then(() => { if (body.isConnected) drawFormatBlock(fmtHost, focus); });
      body.addEventListener('change', (e) => {
        if (e.target.id === 'gd-strategy') {
          fmtEdit.strategy = e.target.value;
          if (fmtEdit.strategy === 'manual' && !fmtEdit.pin) {
            fmtEdit.pin = (fmtEdit.plan && fmtEdit.plan.buckets && fmtEdit.plan.buckets.length)
              ? fmtEdit.plan.buckets[0].label : null;
          }
          drawFormatBlock(fmtHost, focus);
        } else if (e.target.id === 'gd-pin') {
          fmtEdit.pin = e.target.value;
          drawFormatBlock(fmtHost, focus);
        }
      });
    }

    // Move the shared recur/one-off picker into the modal via mountScheduleFields
    // (static/js/schedule-fields.js) rather than wiring its inputs by hand here - one
    // copy of that recur/one-off logic, shared with check-modal.js's schedule block.
    const schedHost = body.querySelector('#gd-sched-fields');
    let gdSched = null;
    if (schedHost) {
      gdSched = mountScheduleFields({
        host: schedHost, templateId: 'gd-schedule-fields', prefix: 'gdsched',      });
      const s = G.schedule || { mode: 'manual' };
      gdSched.prefill({
        oneoff_value: s.oneoff_value, recur_day: s.recur_day, recur_time: s.recur_time,
        use_window: s.use_window,
      });
      gdSched.setMode(s.mode);
      body.querySelector('#gd-sched-mode').addEventListener('change', (e) => gdSched.setMode(e.target.value));
    }

    const modal = buildModal({
      title: 'Settings',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => { saveSettings(body, close, gdSched); return false; } },
      ],
    });
    modal.querySelector('.modal-panel').classList.add('modal-wide');
  }

  // "1920x1080 @ 60" back into the {resolution, fps} pair the /format endpoint takes.
  // format_label() on the server is what produced the string, so this is its inverse and
  // the only place that reads it apart - a second parse elsewhere would be two spellings
  // of one format.
  function parseFormatLabel(label) {
    const m = /^(\d+x\d+)\s*@\s*(\d+)$/.exec(String(label || '').trim());
    return m ? { resolution: m[1], fps: Number(m[2]) } : null;
  }

  // Each block is its own request - the strategy, the pinned lock, the warnings, the
  // schedule and the profile are five different endpoints, and one failing must not
  // silently swallow the others.
  function saveSettings(body, close, gdSched) {
    const reqs = [];
    if (G.hasChannel && body.querySelector('#gd-fmt-block')) {
      // Pinning is ONE request: /format sets the `manual` strategy along with the lock,
      // because writing a pin by hand is what choosing manual means (dev/changelog/762).
      // This used to be /format chained to /format-strategy, and a failed second request
      // left the pin stranded under the old strategy - filtering members forever while
      // this card named a strategy that follows the data instead.
      if (fmtEdit.strategy === 'manual') {
        const pin = parseFormatLabel(fmtEdit.pin);
        if (!pin) {
          showToast('Pick a format to pin, or choose a strategy that follows the data.',
                    { type: 'error' });
          return;
        }
        reqs.push(jsonFetch(api('format'), { method: 'POST', body: JSON.stringify(pin) }));
      } else if (fmtEdit.strategy !== strategyValue()) {
        reqs.push(jsonFetch(api('format-strategy'), {
          method: 'POST', body: JSON.stringify({ strategy: fmtEdit.strategy }),
        }));
      }

      const warnEls = Array.from(body.querySelectorAll('[data-warn]'));
      if (warnEls.length) {
        const muted = new Set((WARN && WARN.muted) || []);
        const changed = warnEls.filter(el => muted.has(el.dataset.warn) === el.checked);
        if (changed.length) {
          const payload = {};
          changed.forEach(el => { payload[el.dataset.warn] = el.checked; });
          reqs.push(jsonFetch(api('warnings'),
                              { method: 'POST', body: JSON.stringify({ warnings: payload }) }));
        }
      }
    }
    if (G.hasCheck) {
      const profileId = body.querySelector('#gd-profile').value || null;
      if (String(profileId) !== String(G.profileId === null ? '' : G.profileId)) {
        reqs.push(jsonFetch(jobApi('profile'), { method: 'POST', body: JSON.stringify({ profile_id: profileId }) }));
      }
      const mode = body.querySelector('#gd-sched-mode').value;
      const cur = (G.schedule || {}).mode;
      if (mode === 'manual') {
        if (cur !== 'manual') reqs.push(jsonFetch(jobApi('unschedule'), { method: 'POST' }));
      } else {
        // gdSched.payload() (schedule-fields.js) is the single author of the once/recur/
        // window shape - hand-reading the day/time/window inputs here would be a second
        // copy of exactly the branching that file exists to centralize.
        const p = gdSched.payload();
        if (p && p.error) { showToast(p.error, { type: 'error' }); return; }
        reqs.push(jsonFetch(jobApi('reschedule'), { method: 'POST', body: JSON.stringify(p) }));
      }
    }
    Promise.all(reqs)
      .then(() => { close(); reloading = true; location.reload(); })
      .catch(e => showToast(e.message || 'Could not save settings.', { type: 'error' }));
  }

  // ── Create health check ───────────────────────────────────────────────────
  // The modal itself lives in check-modal.js - the Groups list opens the same one.

  function openCreateCheck() {
    openCreateCheckModal({
      groupId: G.groupId,
      groupName: G.groupName,
      memberCount: TOTAL,   // live count, not the server-rendered one - rows can change under the page
      profiles: G.checkProfiles.profiles,
      profilesUrl: G.profilesUrl,
      inheritedCheck: G.inheritedCheck,
      hasOwnCheck: G.hasCheck,
      inGuide: G.inGuide,
      testerBusy: G.testerBusy,
      windowSettingsUrl: G.windowSettingsUrl,
      scheduleTemplateId: 'cc-schedule-fields',
      schedulePrefix: 'ccsched',
      // A health check is a schedule its group carries, so it has nothing to name - the
      // route derives the job name from the group (dev/changelog/831).
      nameless: true,
      onDone: () => { reloading = true; location.reload(); },
    });
  }

  // ── Row + page actions ────────────────────────────────────────────────────

  function rowById(cid) { return ROWS.find(r => r.channel_id === cid); }

  function openRowMenu(cid, trigger) {
    const r = rowById(cid);
    if (!r) return;
    const items = [];
    items.push({ act: 'details', label: 'Channel details' });
    if (G.hasCheck) items.push({ act: 'retest', label: 'Test this feed now' });
    if (!r.in_guide) items.push({ act: 'add-guide', label: 'Add to TV Guide' });
    items.push({ act: 'group', label: 'Group with duplicates…' });
    items.push({ sep: true });
    // A stored group's member has its two switches in its own row, so the kebab carries
    // no participation item: a third control writing the same columns from a third place
    // is what the one-writer rule exists to prevent. The health check's own channel list
    // keeps its toggle, which writes Channel.test_enabled through the job route.
    if (!G.hasChannel) {
      items.push({ act: 'toggle', label: r.disabled ? 'Enable here' : 'Disable here' });
    }
    if (!G.isSystem) {
      items.push({ act: 'remove', label: G.hasChannel ? 'Remove from group' : 'Remove from check', danger: true });
    }

    const menu = document.createElement('div');
    menu.className = 'menu open gd-rowmenu';
    menu.innerHTML = items.map(it => it.sep ? '<div class="sep"></div>' :
      `<button class="menu-item${it.danger ? ' danger' : ''}" data-rowact="${it.act}">${escHtml(it.label)}</button>`).join('');
    document.body.appendChild(menu);
    positionMenu(menu, trigger);
    syncScrollLock();
    const dismiss = () => {
      menu.remove();
      document.removeEventListener('click', onDoc, true);
      syncScrollLock();
    };
    const onDoc = (e) => {
      const b = e.target.closest('[data-rowact]');
      if (b && menu.contains(b)) { e.preventDefault(); e.stopPropagation(); dismiss(); rowAction(b.dataset.rowact, r); return; }
      dismiss();
    };
    setTimeout(() => document.addEventListener('click', onDoc, true), 0);
  }

  function rowAction(act, r) {
    if (act === 'details') { location.href = `/channels/${r.channel_id}`; return; }  // nav-ok: kebab menu item
    if (act === 'group') {
      openGroupModal({
        channels: [{
          channel_id: r.channel_id, channel_name: r.channel_name,
          // The account carries through so the picked list draws its dot: which provider a
          // feed came from is what tells two same-named copies apart.
          account_color: r.account_color, account_name: r.account_name,
        }],
        onDone: () => refreshRows(),
      });
      return;
    }
    if (act === 'add-guide') {
      jsonFetch(`/api/guide/channels/${r.channel_id}/add`, { method: 'POST' })
        .then(data => {
          if (data.success) { showToast(`"${r.channel_name}" added to the TV Guide.`); refreshRows(); }
          else showToast(data.error || 'Already in the guide, or needs confirmation from Channel Search.', { type: 'warning' });
        })
        .catch(e => showToast(e.message || 'Could not add to the guide.', { type: 'error' }));
      return;
    }
    if (act === 'retest') {
      jsonFetch(jobApi('test-selected'), { method: 'POST', body: JSON.stringify({ channel_ids: [r.channel_id] }) })
        .then(() => { reloading = true; location.reload(); })
        .catch(e => showToast(e.message || 'Could not start the test.', { type: 'error' }));
      return;
    }
    if (act === 'toggle') {
      // Only ever offered on the system group / a check's own channel list now - a stored
      // group's member is switched in its own row (openRowMenu says why).
      jsonFetch(jobApi(`channels/${r.channel_id}/toggle`), { method: 'POST' })
        .then(() => { showToast(r.disabled ? `"${r.channel_name}" re-enabled.` : `"${r.channel_name}" disabled here.`); refreshRows(); })
        .catch(e => showToast(e.message || 'Request failed.', { type: 'error' }));
      return;
    }
    if (act === 'remove') {
      const what = G.hasChannel
        ? `Remove "${r.channel_name}" from this group? It is not deleted - it stays in your channel list and returns to the guide as an individual channel if it was in it before.`
        : `Remove "${r.channel_name}" from this health check? Its results for this check are deleted.`;
      if (!confirm(what)) return;
      removeMember(r, false);
    }
  }

  // §15 breach path 3, the single-member half: a member LEAVING empties the
  // recording-enabled set exactly as unticking it does, so it takes the same gate and the
  // same dialog rather than a second, weaker check of its own.
  function removeMember(r, confirmed) {
    const req = G.hasChannel
      ? jsonFetch(api('members/remove'), {
          method: 'POST',
          body: JSON.stringify({ channel_id: r.channel_id, confirm: !!confirmed }),
        })
      : jsonFetch(jobApi(`channels/${r.channel_id}`), { method: 'DELETE' });
    req.then((resp) => {
      showToast(`"${r.channel_name}" removed.` + (resp && resp.left_guide
        ? ` ${G.groupName} has left the TV Guide - it was the last member switched on for ` +
          'recording' + (resp.cancelled_recordings
          ? `, and ${plural(resp.cancelled_recordings, 'scheduled recording')} was cancelled.`
          : '.')
        : ''), { type: (resp && resp.left_guide) ? 'warning' : 'success' });
      refreshRows();
    }).catch(e => {
      if (handleInvariantError(e, () => removeMember(r, true), () => {})) return;
      showToast(e.message || 'Request failed.', { type: 'error' });
    });
  }

  function pageAction(act, el) {
    switch (act) {
      // The phone's chrome. Every one of these is a rearrangement of a control the desktop
      // already has, so none of them reaches a route of its own.
      case 'overflow': openOverflow(); return;
      case 'bulk-sheet': openBulkSheet(); return;
      case 'select-done': setSelecting(false); return;
      case 'select-all-visible': {
        const vis = selectableVisible();
        // The one button toggles, because on a phone there is no header checkbox showing
        // an indeterminate state to explain a one-way "All".
        const all = vis.length > 0 && vis.every(r => selected.has(r.channel_id));
        vis.forEach(r => { if (all) selected.delete(r.channel_id); else selected.add(r.channel_id); });
        renderList();
        return;
      }
      case 'test-selected': closeSheet(); byId('gd-test-selected').click(); return;
      case 'delete-missing-selected': closeSheet(); openDeleteMissingSelected(); return;
      case 'settings': openSettingsModal(el && el.dataset.focus); return;
      case 'mute': muteWarning(el.dataset.mute); return;
      case 'review-members': reviewMembers(el && el.dataset.review); return;
      // The EPG banner's per-id links. Same function, told which id - `dataset.epg` is the
      // empty string on the "no EPG id at all" link, which is a bucket and not a missing
      // argument, so it must not be collapsed into the no-id-named case above.
      case 'review-epg': reviewMembers('epg', el.dataset.epg); return;
      case 'schedule': openSettingsModal('check'); return;
      case 'sections': sectionLayout.open(); return;
      case 'dedup': openDedup(); return;
      case 'delete-missing': openDeleteMissing(); return;
      case 'create-check': openCreateCheck(); return;
      // No 'create-group' case: promoting a health check into a channel group was the
      // two-object model's only reason to exist, and there is one object now
      // (DESIGN-channel-groups-model.md DECIDED 2, dev/changelog/752). Cloning a group
      // still opens that modal - from clone-modal.js, not here.
      case 'pick-format':
        openFormatPickerModal({
          groupId: G.groupId,
          groupName: G.groupName,
          channels: ROWS.map(r => ({ id: r.channel_id, name: r.channel_name })),
          onDone: () => { reloading = true; location.reload(); },
        });
        return;
      case 'run-linked-check':
        postAndReload(`/api/channel-tests/on-demand/${el.dataset.job}/start`);
        return;
      case 'suggest':
        openGroupModal({ channels: [], fixedGroup: { id: G.groupId, name: G.groupName } });
        return;
      case 'rename':
        openRenameModal();
        return;
      case 'guide-toggle': {
        // §14.1's first trigger. The guide button on a group that is still a health check
        // does not refuse - it asks the format question and then does what was clicked.
        // Taking a group OUT of the guide is never gated: nothing is lost by it.
        if (!G.inGuide && needsPromotion()) { openWalkthrough('guide'); return; }
        showActionError('');
        jsonFetch(api('guide-toggle'), { method: 'POST' })
          .then(() => { reloading = true; location.reload(); })
          .catch(err => {
            // §15 breach path 1, refused server-side. The dialog offers the fix rather
            // than only naming the problem, which is what makes blocking the right answer
            // here instead of a warning.
            if (handleInvariantError(err, null, null)) return;
            showActionError(err.message || 'Request failed.');
          });
        return;
      }
      case 'walkthrough': openWalkthrough('manual'); return;
      case 'clone':
        openCloneModal(G.groupId, {
          existingNames: G.groupNames,
          resolutionOptions: G.resolutionOptions,
          fpsOptions: G.fpsOptions,
          profiles: G.checkProfiles.profiles,
          profilesUrl: G.profilesUrl,
          testerBusy: G.testerBusy,
          windowSettingsUrl: G.windowSettingsUrl,
          scheduleTemplateId: 'cc-schedule-fields',
          schedulePrefix: 'ccsched',
          onDone: () => { reloading = true; location.reload(); },
        });
        return;
      case 'delete-group':
        // The dialog, the live-capture refusal and the scheduled-recordings confirm all
        // live in group-delete.js, shared with the groups list - both pages offer the
        // same destructive action and must describe it the same way.
        openDeleteGroupModal({
          groupName: G.groupName,
          deleteUrl: api('delete'),
          attachedChecks: G.checks || [],
          onDeleted: () => { reloading = true; location.href = G.groupsUrl; },  // nav-ok: redirect after deleting the group
          onError: (msg) => showActionError(msg),
        });
        return;
      case 'start': postAndReload(jobApi('start')); return;
      case 'run-now': {
        // Matches jobs.js's runPrompt() keep-prompt branch: three explicit
        // buttons instead of a confirm() that overloaded Cancel to mean "run
        // it, and drop the schedule" - the opposite of what Cancel says.
        const nextRun = (G.schedule && G.schedule.next_run_et) || 'time unknown';
        buildModal({
          title: `Run ${escHtml(G.jobName || 'this health check')} now`,
          body: `<p>This check is scheduled to run again at <strong>${escHtml(nextRun)}</strong>. ` +
                'Running it now does not change that unless you say so.</p>',
          footer: [
            { label: 'Cancel', class: 'btn' },
            {
              label: 'Run and drop the schedule',
              class: 'btn',
              onClick: (c) => { c(); postAndReload(jobApi('start'), 'POST', { keep_schedule: false }); },
            },
            {
              label: 'Run and keep it',
              class: 'btn btn-primary',
              onClick: (c) => { c(); postAndReload(jobApi('start'), 'POST', { keep_schedule: true }); },
            },
          ],
        });
        return;
      }
      case 'restart':
        if (!confirm('Test again and re-run all channels? Prior results are kept in each channel\'s history, not deleted.')) return;
        postAndReload(jobApi('restart'));
        return;
      case 'start-over':
        if (!confirm('Start over and re-test all channels? Prior results are kept in each channel\'s history, not deleted.')) return;
        postAndReload(jobApi('restart'));
        return;
      case 'resume': postAndReload(jobApi('resume')); return;
      case 'force-cancel':
        if (!confirm('Force cancel this check? It will be marked Cancelled and you can then Resume or Start over.')) return;
        postAndReload(jobApi('force-cancel'));
        return;
      case 'unschedule':
        if (!confirm('Remove this schedule? The health check itself is kept as-is.')) return;
        postAndReload(jobApi('unschedule'));
        return;
      case 'pause-schedule': postAndReload(jobApi('pause-schedule')); return;
      case 'resume-schedule': postAndReload(jobApi('resume-schedule')); return;
      case 'skip-next':
        if (!confirm('Skip the next scheduled run? It runs at the following occurrence instead.')) return;
        postAndReload(jobApi('skip-next'));
        return;
      case 'stop': {
        const btn = el;
        if (btn) { btn.disabled = true; btn.textContent = 'Stopping…'; }
        jsonFetch(jobApi('stop'), { method: 'POST' })
          .catch(e => showActionError(e.message || 'Could not stop the run.'))
          .finally(() => setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = 'Stop'; } }, 3000));
        return;
      }
      default: showToast(`No handler for "${act}" - that is a bug.`, { type: 'error' });
    }
  }

  // ── Participation: the two switches, single and bulk ──────────────────────
  //
  // Both write through the same route, which writes through the app's single
  // set_participation() - so every move lands in the group's Activity Timeline whichever
  // control was used. Nothing here decides anything the server does not re-check.

  // ── §15's invariant, client side ──────────────────────────────────────────
  //
  // Nothing here enforces anything - the routes do, and they refuse whether or not this
  // code asks first. What this half owns is the sentence: a 409 carrying
  // `confirm_required` or `recording_in_progress` is a server refusal with the facts
  // attached, and these turn those facts into the dialog DESIGN.md §4 describes.

  // A plain confirm on the app's own buildModal. Sentence 1 says what will happen,
  // sentence 2 says the consequence, and the confirm button is named the action verb
  // rather than "OK".
  function confirmModal(o) {
    const body = document.createElement('div');
    body.innerHTML = o.lines.map(l => `<p class="card-note">${l}</p>`).join('');
    return buildModal({
      title: o.title,
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => { c(); if (o.onCancel) o.onCancel(); } },
        { label: o.confirm, class: o.confirmClass || 'btn btn-danger',
          onClick: (c) => { c(); o.onConfirm(); } },
      ],
    });
  }

  // §15.1. A capture running right now is not something a checkbox may end. The dialog
  // has one way forward and it is the deliberate verb: go to the recording and abort it.
  function liveRecordingModal(info, onCancel) {
    return confirmModal({
      title: 'This group is recording right now',
      lines: [
        `<strong>${escHtml(G.groupName)}</strong> is recording ${escHtml(info.name || '')} ` +
        `until ${escHtml(info.until || 'later')}.`,
        'Turning off the last member it can record from would leave that recording with ' +
        'nowhere to fail over to. Let it finish, or abort it deliberately, and then come ' +
        'back to this.',
      ],
      confirm: 'Open the recording',
      confirmClass: 'btn btn-primary',
      onCancel,
      onConfirm: () => { location.href = `/recordings/${info.recording_id}`; },  // nav-ok: confirm-dialog button
    });
  }

  // §15, breach paths 2 and 3. One dialog for one switch, a selection, or a removal -
  // what matters is that the group ends up with nothing to record from, not how many
  // controls the user moved to get there.
  function confirmLastMember(facts, retry, onCancel) {
    const names = facts.losing || [];
    const many = names.length > 1;
    const who = many
      ? `The ${names.length} selected members are`
      : `<strong>${escHtml((names[0] || {}).channel_name || 'That member')}</strong> is`;
    const lines = [`${who} the last ${many ? 'members' : 'member'} of ` +
      `<strong>${escHtml(facts.group_name || G.groupName)}</strong> switched on for recording.`];
    if (facts.in_guide) {
      lines.push('Turning it off removes this group from the TV Guide, because a guide row ' +
        'with nothing to record from is a row that cannot produce a file.');
      if (facts.scheduled_count) {
        lines.push(`${plural(facts.scheduled_count, 'scheduled recording')} on this group ` +
          `${facts.scheduled_count === 1 ? 'would be' : 'would be'} cancelled as well.`);
      }
    } else {
      lines.push('This group is not in the TV Guide, so nothing else changes - it goes back ' +
        'to being a health check.');
    }
    return confirmModal({
      title: 'Turn off the last recording member?',
      lines,
      confirm: facts.in_guide ? 'Turn it off and remove from the guide' : 'Turn it off',
      onCancel,
      onConfirm: retry,
    });
  }

  // The one place a 409 from any of the participation/removal routes is read. Returns
  // true when it handled the error, so callers fall through to their own error toast for
  // everything else.
  function handleInvariantError(err, retry, onCancel) {
    const d = err && err.data;
    if (!d) return false;
    if (d.recording_in_progress) { liveRecordingModal(d.recording_in_progress, onCancel); return true; }
    if (d.confirm_required) { confirmLastMember(d.confirm_required, retry, onCancel); return true; }
    if (d.needs_recording_member) {
      confirmModal({
        title: 'Turn on recording for at least one member',
        lines: [
          'A TV Guide row exists to be recorded from, and no member of ' +
          `<strong>${escHtml(G.groupName)}</strong> is switched on for recording.`,
          'Turn Recording on for at least one member, then add the group to the guide.',
        ],
        confirm: 'Set them up now',
        confirmClass: 'btn btn-primary',
        onCancel,
        onConfirm: () => openWalkthrough('guide'),
      });
      return true;
    }
    return false;
  }

  // §14.1's dialog, with the page's own data handed to it. Opened by the guide button,
  // by the first Recording switch on a health-check-only group, and by the bulk
  // Recording-on action - all three complete the click that opened them.
  function openWalkthrough(trigger, pendingIds) {
    openPromoteModal({
      groupId: G.groupId,
      groupName: G.groupName,
      rows: ROWS,
      derived: G.derivedReferenceLabel || null,
      inGuide: !!G.inGuide,
      trigger,
      pendingIds: pendingIds || [],
      onDone: () => { reloading = true; location.reload(); },
    });
  }

  // Is this group still a health check rather than a recording source? The trigger for
  // the walkthrough, read from the same server-decided field the banners are gated on.
  function needsPromotion() {
    return G.hasChannel && strategyValue() === 'health_check_only';
  }

  // What the user is owed after a switch moves. Turning a switch OFF is the half that
  // needs saying: the row keeps its place and nothing else about the member changed, so
  // without a sentence the only feedback is a knob sliding.
  function partToast(r, which, on, resp) {
    if (which === 'test') {
      if (!on && r.recording_enabled) {
        showToast(`"${r.channel_name}" is switched on for recording and will no longer be checked. ` +
          'ChannelBin may pick it for a recording without knowing whether it still works.',
        { type: 'warning' });
      } else {
        showToast(`"${r.channel_name}" is ${on ? 'included in' : 'excluded from'} this group's health check.`);
      }
      return;
    }
    if (on) {
      showToast(`"${r.channel_name}" is eligible for recording.` +
        (r.test_enabled ? '' : ' It is not being health checked, so nothing knows whether it works.'),
      { type: r.test_enabled ? 'success' : 'warning' });
      return;
    }
    // Whether this emptied the group is the SERVER's answer (`left_guide`), not a count
    // of the rows this page happens to be holding - the two disagree the moment another
    // tab moves a switch, and this sentence is the record of what actually happened.
    const left = ROWS.filter(x => x.recording_enabled && x.channel_id !== r.channel_id).length;
    showToast(`"${r.channel_name}" will not be picked for a recording. Nothing else about it changed.` +
      (resp && resp.left_guide
        ? ` ${G.groupName} has left the TV Guide` +
          (resp.cancelled_recordings
            ? `, and ${plural(resp.cancelled_recordings, 'scheduled recording')} was cancelled.`
            : '.')
        : (left ? '' : ' No member is enabled for recording now, so nothing can record from ' +
          'this group.')),
    { type: (resp && resp.left_guide) || !left ? 'warning' : 'success' });
  }

  function changePart(cid, which, on, confirmed) {
    const r = rowById(cid);
    if (!r) return;
    const field = PART_COLS[which];
    // §14.1's second trigger: the first Recording switch on a group that is still a
    // health check asks the format question, then completes the switch that opened it.
    if (which === 'rec' && on && needsPromotion()) {
      renderList();      // put the knob back until the walkthrough writes it
      openWalkthrough('member', [cid]);
      return;
    }
    jsonFetch(api('members/participation'), {
      method: 'POST',
      body: JSON.stringify({ channel_id: cid, field, enabled: on, confirm: !!confirmed }),
    }).then((resp) => {
      partToast(r, which, on, resp);
      // Re-read rather than patching the row locally: turning a switch can move the
      // group's derived format reference, which changes who is format-blocked and which
      // member carries the star - facts this file does not compute.
      refreshRows();
    }).catch(err => {
      // §15: a refusal carrying the facts, turned into the dialog that names them. Both
      // the cancel path and the unhandled path put the knob back where the server has it.
      const revert = () => renderList();
      if (handleInvariantError(err, () => changePart(cid, which, on, true), revert)) {
        revert();
        return;
      }
      showToast(err.message || 'Could not change that setting.', { type: 'error' });
      revert();
    });
  }

  function bulkPart(which, on, confirmed) {
    const ids = selectableVisible().filter(r => selected.has(r.channel_id)).map(r => r.channel_id);
    if (!ids.length) return;
    const field = PART_COLS[which];
    const label = COL_LABEL[which];
    // §14.1's trigger reached from a selection rather than from one switch. The
    // walkthrough finishes the job it interrupted, for the whole selection.
    if (which === 'rec' && on && needsPromotion()) {
      openWalkthrough('bulk', ids);
      return;
    }
    jsonFetch(api('members/participation/bulk'), {
      method: 'POST',
      body: JSON.stringify({ channel_ids: ids, field, enabled: on, confirm: !!confirmed }),
    }).then(data => {
      const moved = data.moved || 0;
      if (data.left_guide) {
        showToast(`${label} turned off for ${plural(moved, 'member')}. ${G.groupName} has ` +
          'left the TV Guide - nothing is switched on for recording any more' +
          (data.cancelled_recordings
            ? `, and ${plural(data.cancelled_recordings, 'scheduled recording')} was cancelled.`
            : '.'), { type: 'warning' });
        return;
      }
      if (!moved) {
        showToast(`All ${plural(ids.length, 'selected member')} already had ` +
          `${label.toLowerCase()} ${on ? 'on' : 'off'}. Nothing changed.`);
        return;
      }
      // The dangerous combination is reachable here one action at a time, so the toast
      // names the result rather than reporting a count and stopping.
      const risky = (which === 'test' && !on)
        ? ids.filter(id => { const r = rowById(id); return r && r.recording_enabled; }).length : 0;
      showToast(`${label} turned ${on ? 'on' : 'off'} for ${plural(moved, 'member')}.` + (risky
        ? ` ${risky} of them ${risky === 1 ? 'is' : 'are'} switched on for recording and will no ` +
          `longer be checked - ChannelBin may pick ${risky === 1 ? 'it' : 'one of them'} for a ` +
          'recording without knowing whether it still works.'
        : ''), { type: risky ? 'warning' : 'success' });
    }).catch(err => {
      // The bulk path takes the SAME §15 dialogs the single switch takes - a guard a
      // bulk action can walk around is not a guard, and this is the half of it the user
      // sees. Nothing is retried without the confirm the server asked for.
      if (handleInvariantError(err, () => bulkPart(which, on, true), () => refreshRows())) return;
      showToast(err.message || 'Could not change those settings.', { type: 'error' });
    }).finally(() => refreshRows());
  }

  // ── Bulk selection ────────────────────────────────────────────────────────

  // Nothing on a stored group is unselectable any more - `disabled` survives only on the
  // system group, whose rows carry Channel.test_enabled rather than a membership.
  function selectableVisible() { return visibleRows().filter(r => !r.disabled); }

  function syncSelection() {
    // Before the guard: the sticky bar carries the group's own actions even when there is
    // nothing selectable, so it is not conditional on there being bulk verbs.
    renderBottomBar();
    if (!G.hasCheck && !G.hasChannel) return;
    const n = selected.size;
    byId('gd-sel-count').textContent = `${n} selected`;
    byId('gd-clear-selection').style.display = n ? '' : 'none';
    const running = !!testerStatus.is_running || G.jobStatus === 'RUNNING';
    const btn = byId('gd-test-selected');
    if (btn) {
      btn.disabled = n === 0 || running;
      btn.title = running ? 'A health check is already running' : '';
      btn.textContent = n ? `▶ Test selected (${n})` : '▶ Test selected';
    }
    // The two bulk participation menus, which exist on any stored group.
    ['gd-bulk-rec', 'gd-bulk-test'].forEach(id => {
      const b = byId(id);
      if (b) b.disabled = n === 0;
    });
    const vis = selectableVisible();
    const sel = vis.filter(r => selected.has(r.channel_id)).length;
    const all = byId('gd-select-all');
    all.checked = vis.length > 0 && sel === vis.length;
    all.indeterminate = sel > 0 && sel < vis.length;
  }

  // ── Live status polling ───────────────────────────────────────────────────

  function refreshRows() {
    const url = api('detail-rows') + (G.jobId ? `?job_id=${G.jobId}` : '');
    return jsonFetch(url)
      .then(data => {
        ROWS = data.rows;
        DUP_GROUPS = data.dup_groups;
        MISSING_CHANNELS = data.missing_channels;
        COUNTS = data.counts;
        TOTAL = data.total;
        // The lock can move under a refresh (a check run re-evaluates the strategy), and
        // 16.1's mismatch pill names it - a stale label here would name a format the group
        // is no longer locked to.
        G.referenceLabel = data.reference_label;
        G.lockLabel = data.lock_label;
        // Moves for the same reason and is a different value: the format the data alone
        // points at, which is what "Healthiest member's format" follows.
        G.derivedReferenceLabel = data.derived_reference_label;
        // Same reason as the lock label above, one level up: moving a Recording switch
        // changes who is format-blocked, which formats the group spans and which EPG ids
        // are in play, so §16's banners are re-decided server-side on every refresh
        // rather than patched here from what the client thinks changed.
        WARN = data.warnings;
        // Drop selections for channels no longer in the table.
        const present = new Set(ROWS.map(r => r.channel_id));
        Array.from(selected).forEach(id => { if (!present.has(id)) selected.delete(id); });
        renderList();
        renderSummary();
        renderBanners();
        renderSettingsBar();
        renderDupBanner();
        renderMissingBanner();
        // The rows just changed underneath the bar, so its account values are re-derived
        // and any filter that no longer matches anything present is dropped with its chip.
        filterBar.render();
      })
      .catch(() => {});
  }

  function updateHero(isMine) {
    const hero = byId('gd-hero');
    const log = byId('gd-log-card');
    hero.style.display = isMine ? '' : 'none';
    log.style.display = isMine ? '' : 'none';
    if (!isMine) return;
    const s = testerStatus;
    const waiting = (s.current_phase || 'testing') === 'waiting';
    byId('gd-hero-phase').textContent = waiting ? 'Between channels - waiting' : 'Testing channel';
    byId('gd-hero-name').textContent = waiting ? (s.next_channel_name ? `Up next: ${s.next_channel_name}` : '') : (s.current_channel_name || '');
    byId('gd-hero-url').textContent = waiting ? '' : (s.current_channel_url || '');
    byId('gd-hs-drops').textContent = s.current_test_drop_count ?? 0;
    byId('gd-hs-data').textContent = fmtBytes(s.current_live_bytes || 0);
    byId('gd-hs-progress').textContent = `${s.completed_channels}/${s.total_channels}`;
    byId('gd-hero-bar').style.width = (s.total_channels > 0 ? Math.round(s.completed_channels / s.total_channels * 100) : 0) + '%';
    byId('gd-hs-elapsed-stat').style.display = waiting ? 'none' : '';
    const wrap = byId('gd-hero-shot-wrap');
    const img = byId('gd-hero-shot');
    if (!waiting && s.current_test_screenshot_url) {
      if (img.src !== location.origin + s.current_test_screenshot_url) img.src = s.current_test_screenshot_url;
      wrap.style.display = '';
    } else {
      wrap.style.display = 'none';
    }
  }

  // Sole live updater for the status bar's message. The bar is server-rendered from the
  // tester's state at page load and had no updater at all, so through a run of 104 channels
  // it sat on "Testing channel 1 of 104" - the one line on the page that says how far along
  // the run is, frozen at whatever was true when the page opened
  // (dev/docs/BUGS.md 2026-08-28 06:14 pm). Phrasing is duplicated in group_detail.html's
  // first frame of this region; the two must move together.
  function updateActionBarMsg(isMine) {
    if (!isMine) return;
    const el = document.querySelector('#gd-inline-actionbar .gd-ab-msg');
    if (!el) return;
    const s = testerStatus;
    el.textContent = (s.current_phase || 'testing') === 'waiting'
      ? `Between channels - ${s.completed_channels} of ${s.total_channels} done.`
      : `Testing channel ${s.completed_channels + 1} of ${s.total_channels}.`;
  }

  // Append-only: rebuilding the node on every poll destroyed any text the user had
  // highlighted. Entries are matched by the server's monotonic `seq`, not by position,
  // because the server buffer evicts from the front past 500 entries.
  function updateLog(logs) {
    const el = byId('gd-test-log');
    if (!el || !logs || !logs.length) return;
    const fresh = logs.filter(e => (e.seq || 0) > lastLogSeq);
    if (fresh.length) {
      const placeholder = el.querySelector('.gd-log-empty');
      if (placeholder) placeholder.remove();
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      fresh.forEach(e => {
        const row = document.createElement('div');
        const t = fmtTimeTz(utcIsoToDate(e.ts), { seconds: true });
        const ts = document.createElement('span');
        ts.style.color = 'var(--text-muted)';
        ts.textContent = `[${t}]`;
        const msg = document.createElement('span');
        msg.style.color = LOG_COLORS[e.level] || 'var(--text-muted)';
        msg.textContent = e.msg;
        row.appendChild(ts);
        row.appendChild(document.createTextNode(' '));
        row.appendChild(msg);
        el.appendChild(row);
        lastLogSeq = Math.max(lastLogSeq, e.seq || 0);
      });
      // Hold the view still and defer trimming while the user has something highlighted.
      if (!hasSelectionIn(el)) {
        if (nearBottom) el.scrollTop = el.scrollHeight;
        while (el.childElementCount > 2000) el.removeChild(el.firstElementChild);
      }
    }
    byId('gd-log-count').textContent = `${el.childElementCount} entr${el.childElementCount === 1 ? 'y' : 'ies'}`;
  }

  function pollStatus() {
    fetch('/api/channel-tests/status')
      .then(r => r.json())
      .then(s => {
        testerStatus = s;
        const isMine = G.hasCheck && s.is_running && s.current_job_id === G.jobId;
        updateHero(isMine);
        updateActionBarMsg(isMine);
        if (isMine) updateLog(s.logs || []);
        const justFinished = wasRunning && !isMine;
        if (isMine || justFinished) refreshRows();
        // A run that STARTED while this page was open leaves the bar server-rendered from
        // the status it had then - the badge still says Scheduled and the action group
        // still offers Run rather than Stop. Only the message has an in-place updater, so
        // the rest needs the same reload the finish already takes. Guarded on jobStatus so
        // a page opened DURING a run (jobStatus already RUNNING) does not reload forever.
        const justStarted = isMine && G.jobStatus !== 'RUNNING';
        // The status badge and its action set are server-rendered per job status and have
        // no in-place JS updater, so a status that has moved on needs a full reload
        // rather than a table that quietly disagrees with the bar above it.
        if ((justFinished || justStarted) && !reloading) { reloading = true; location.reload(); return; }
        wasRunning = isMine;
        clearTimeout(pollTimer);
        pollTimer = setTimeout(pollStatus, isMine ? 2000 : (G.jobStatus === 'RUNNING' ? 3000 : 15000));
      })
      .catch(() => { clearTimeout(pollTimer); pollTimer = setTimeout(pollStatus, 5000); });
  }

  setInterval(() => {
    if (!testerStatus.is_running || testerStatus.current_job_id !== G.jobId) return;
    const now = Date.now();
    if (testerStatus.current_phase === 'testing' && testerStatus.current_test_started_at) {
      byId('gd-hs-elapsed').textContent = fmtDur((now - new Date(testerStatus.current_test_started_at + 'Z').getTime()) / 1000);
    }
    if (testerStatus.run_started_at) {
      byId('gd-hs-run-elapsed').textContent = fmtDur((now - new Date(testerStatus.run_started_at + 'Z').getTime()) / 1000);
    }
  }, 1000);

  // ── Wiring ────────────────────────────────────────────────────────────────

  document.addEventListener('click', (e) => {
    const shot = e.target.closest('[data-shot]');
    if (shot) { openLightbox(shot.dataset.shot); return; }

    const rowMenuBtn = e.target.closest('[data-rowmenu]');
    if (rowMenuBtn) { e.preventDefault(); openRowMenu(Number(rowMenuBtn.dataset.rowmenu), rowMenuBtn); return; }

    const expandBtn = e.target.closest('[data-expand]');
    if (expandBtn) {
      const id = Number(expandBtn.dataset.expand);
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      renderList();
      return;
    }

    const sortTh = e.target.closest('[data-sort]');
    if (sortTh) {
      const k = sortTh.dataset.sort;
      if (sortKey === k) sortDir *= -1;
      else { sortKey = k; sortDir = (k === 'name') ? 1 : -1; }
      renderList();
      return;
    }

    // The paired-half badge scrolls rather than navigates: one page carries both halves,
    // so there is nowhere else to go.
    const jump = e.target.closest('[data-jump]');
    if (jump) {
      const target = byId(jump.dataset.jump);
      if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        target.classList.add('gd-jump-flash');
        setTimeout(() => target.classList.remove('gd-jump-flash'), 1200);
      }
      return;
    }

    // ── The phone's chips and sheet rows ───────────────────────────────────
    const chip = e.target.closest('#gd-chip-sort, #gd-chip-filter, #gd-chip-select');
    if (chip) {
      e.preventDefault();
      if (chip.id === 'gd-chip-sort') openSort();
      else if (chip.id === 'gd-chip-filter') openFilters();
      else setSelecting(!selecting);
      return;
    }

    const sortPick = e.target.closest('[data-sortpick]');
    if (sortPick) {
      e.preventDefault();
      const k = sortPick.dataset.sortpick;
      if (sortKey === k) sortDir *= -1;
      else { sortKey = k; sortDir = (k === 'name') ? 1 : -1; }
      afterSortChange();
      return;
    }

    // Every filter row in the sheet, whichever dimension it belongs to. Tapping the value
    // already on turns it off, which is the only way a sheet row can be un-chosen - there
    // is no "All accounts" row to go back to.
    const fPick = e.target.closest('[data-fpick]');
    if (fPick) {
      e.preventDefault();
      filterBar.toggle(fPick.dataset.fpick, fPick.dataset.fpickval);
      afterFilterChange();
      return;
    }

    const fieldPick = e.target.closest('[data-fieldpick]');
    if (fieldPick) {
      e.preventDefault();
      const k = fieldPick.dataset.fieldpick;
      toggleField(k, !fieldOn(k));
      // The desktop Columns popover reads the same `colState`, so it is rebuilt here as
      // well: two pickers over one set must never disagree about what is on.
      buildColMenu();
      afterFilterChange();
      return;
    }

    const bulk = e.target.closest('[data-bulk]');
    if (bulk) {
      e.preventDefault();
      const [which, dir] = bulk.dataset.bulk.split(':');
      closeMenus();
      // The same values from the desktop dropdown and from the phone's Actions sheet, so
      // both reach the one bulk route and therefore the one writer.
      closeSheet();
      bulkPart(which, dir === 'on');
      return;
    }

    const act = e.target.closest('[data-act]');
    if (act) { e.preventDefault(); pageAction(act.dataset.act, act); return; }

    if (e.target.id === 'gd-search-clear') {
      byId('gd-search').value = '';
      searchTerm = '';
      byId('gd-search-wrap').classList.remove('has-text');
      renderList();
      return;
    }

    // Row-expand fallback, deliberately LAST: every real action inside the row (the
    // name link, the checkbox, Details, the kebab) is claimed above, so whatever is
    // left over is dead space and opens the profile drawer.
    //
    // `label.switch` has to be excluded by name. A .switch paints its .knob over its own
    // input (`position: absolute; inset: 0`, declared after it), so a real click lands on
    // the knob - which is a SIBLING of the input, not an ancestor, and `closest('input')`
    // misses it. Without this the fallback fired first and redrew the list, replacing
    // the tbody before the label's forwarded activation could reach the live input: the
    // row expanded, the switch sprang back, and nothing was ever posted.
    const row = e.target.closest('.gd-row');
    if (row && !row.classList.contains('no-profile') &&
        !e.target.closest('a, button, input, label.switch')) {
      const id = Number(row.dataset.cid);
      if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
      renderList();
    }
  });

  document.addEventListener('change', (e) => {
    const part = e.target.closest('[data-part]');
    if (part) { changePart(Number(part.dataset.cid), part.dataset.part, part.checked); return; }
    if (e.target.id === 'gd-select-all') {
      selectableVisible().forEach(r => {
        if (e.target.checked) selected.add(r.channel_id); else selected.delete(r.channel_id);
      });
      renderList();
      return;
    }
    const sel = e.target.closest('[data-sel]');
    if (sel) {
      const id = Number(sel.dataset.sel);
      if (sel.checked) selected.add(id); else selected.delete(id);
      syncSelection();
    }
  });

  const searchInput = byId('gd-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      searchTerm = searchInput.value;
      byId('gd-search-wrap').classList.toggle('has-text', !!searchTerm);
      renderList();
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || !searchInput.value) return;
      searchInput.value = '';
      searchTerm = '';
      byId('gd-search-wrap').classList.remove('has-text');
      renderList();
    });
  }

  const clearSel = byId('gd-clear-selection');
  if (clearSel) clearSel.addEventListener('click', () => { selected.clear(); renderList(); });

  const testSelected = byId('gd-test-selected');
  if (testSelected) {
    testSelected.addEventListener('click', () => {
      const ids = Array.from(selected);
      if (!ids.length) return;
      const err = byId('gd-bulk-error');
      err.style.display = 'none';
      testSelected.disabled = true;
      testSelected.textContent = 'Starting…';
      jsonFetch(jobApi('test-selected'), { method: 'POST', body: JSON.stringify({ channel_ids: ids }) })
        .then(() => { reloading = true; location.reload(); })
        .catch(e => {
          err.textContent = e.message || 'Request failed.';
          err.style.display = '';
          syncSelection();
        });
    });
  }

  const logCopy = byId('gd-log-copy');
  if (logCopy) {
    logCopy.addEventListener('click', async () => {
      const el = byId('gd-test-log');
      const rows = [...el.children].filter(r => !r.classList.contains('gd-log-empty'));
      if (!rows.length) { showToast('Nothing to copy.', { type: 'error' }); return; }
      try {
        await navigator.clipboard.writeText(rows.map(r => r.textContent).join('\n'));
        showToast(`Copied ${plural(rows.length, 'log line')}.`);
      } catch (err) {
        showToast('Copy failed - clipboard access was denied.', { type: 'error' });
      }
    });
  }

  // ── Start ─────────────────────────────────────────────────────────────────

  buildColMenu();
  renderSettingsBar();
  renderSummary();
  renderBanners();
  renderDupBanner();
  renderMissingBanner();
  renderList();
  initBarReveal();
  pollStatus();

  // Crossing the breakpoint redraws everything that branches on it. Without this a phone
  // rotated to landscape - or a desktop window dragged narrow - keeps whichever drawing it
  // booted with, and the one on screen is then rendered by rules meant for the other.
  // Selection mode is a phone concept and does not survive leaving it.
  const onBreakpointChange = () => {
    if (!isPhone() && selecting) { selecting = false; selected.clear(); }
    closeSheet();
    renderSettingsBar();
    renderList();
  };
  if (MOBILE_MQ.addEventListener) MOBILE_MQ.addEventListener('change', onBreakpointChange);
  else if (MOBILE_MQ.addListener) MOBILE_MQ.addListener(onBreakpointChange);
})();
