/* The "+ Group selected" flow: three screens that only go forwards, ending in one
   Create that writes the group and its health check together.

   Approved as dev/mockups/37-group-create-flow-desktop.html (five rounds) and
   38-group-create-flow-mobile.html (two); ported in dev/changelog/831. It replaces the
   pair of bulk actions the channel search bar used to carry - "Test selected" and "Group
   selected" - which since the groups revamp did substantially the same job behind two
   modals that looked nothing alike. Screen 3 is static/js/check-modal.js, the shipped
   modal, opened with the flow's own options.

   Four things about it are load-bearing and must not drift:

   1. NOTHING IS WRITTEN UNTIL THE LAST SCREEN'S CREATE. Backing out of screen 3 - Back,
      the X, the backdrop, Escape - leaves no half-made group behind. An earlier round
      created the group on screen 2 and every dismissal of screen 3 left one lying around.
   2. CREATING A GROUP WARNS ABOUT NOTHING. Not format, not the guide, not recording. A
      brand new group is in no guide, is recorded from by nothing, and has no locked
      format, so none of the choices a lock governs exist yet.
   3. The ONE TRUE WARNING is adding channels to a group that is ALREADY in the guide and
      ALREADY carries a stored format lock - there the mismatch has a consequence today
      (DESIGN-channel-groups-model.md §5), so it is said before the click. Two buckets,
      and they are not the same claim: a measured format that differs is proven different,
      while a channel with no health data is unknown, and unknown is never filtered out.
   4. A ← Back never costs you your work. Screen 2's typed name and picked mode ride
      through screen 1 and come back.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, fieldRow),
   check-modal.js (openCreateCheckModal) and group-modal.js (groupWarningsHtml,
   cgPickedHtml) - the two this file draws from the shared modal rather than owning, so
   the search page and the group detail page cannot come to disagree about either.
*/

/* The intro screen's dismissal is stored as the VERSION the user dismissed, never a
   boolean: bump this constant in a release that changes what the screen says and everyone
   who dismissed an older one sees it once more. It lives in the generic server-side
   user-prefs store (DESIGN.md §3.11) rather than localStorage, so it follows the user
   between browsers. */
const GROUP_INTRO_VERSION = 1;
const GROUP_INTRO_PREF_KEY = 'group_intro_seen_version';

/* What a channel group is for. DESCRIBED, never picked: an earlier round offered these as
   a choice and the answer steered nothing but wording - both roads build the identical
   row, and every switch either one implies lives on the group's own page afterwards. */
const CG_USES = [
  {
    title: 'Test these channels',
    blurb: 'Perform a health check on these channels to determine if they work and to gather '
      + 'details such as resolution, frame rate, and more. Can be performed manually or on a '
      + 'recurring schedule.',
  },
  {
    title: 'Combine into one channel',
    blurb: 'Combine multiple copies of a channel, even across different accounts, into one. '
      + 'That channel group can be added to the TV Guide and used for recordings as if it\'s '
      + 'one channel. ChannelBin will start with the best channel based on your settings, and '
      + 'will automatically fail over between others in the group if needed. Channel groups '
      + 'that will be used for recording should have regularly scheduled health checks so that '
      + 'ChannelBin knows the status of each channel within the group.',
  },
];

/* One width for all three screens, so the flow is one box that changes contents rather
   than three boxes of different sizes. .modal-xwide is an existing step in style.css, and
   at the narrower one the last screen's longest option clipped inside its own control. */
const CG_PANEL_CLASS = 'modal-xwide cg-flow';

/* The flow closes back onto the search page with the same rows still ticked - the common
   next move after grouping a search result is to group another one out of the same
   results, and clearing the selection threw that away. The link is there for the times
   the group IS where you wanted to end up, so the toast holds far longer than the 5s
   default: a link nobody has time to click is decoration. */
const CG_TOAST_MS = 12000;

/* Words (case-insensitive) common to every selected name, in the first name's order. On a
   set of copies of one channel those common words ARE the channel, which is what the rule
   was written for; on an unrelated set they produce a word or two of noise, so a single
   common word is treated as no answer and the field starts empty with its placeholder. */
function cgPrefillName(rows) {
  if (!rows.length) return '';
  const wordSets = rows.map((r) => new Set(String(r.channel_name).toLowerCase().split(/\s+/).filter(Boolean)));
  const common = String(rows[0].channel_name).split(/\s+/)
    .filter((w) => wordSets.every((s) => s.has(w.toLowerCase())));
  return common.length > 1 ? common.join(' ').trim() : '';
}

function cgLockedFormat(g) {
  return (g && g.format_resolution && g.format_fps != null)
    ? `${g.format_resolution} @ ${g.format_fps}` : null;
}

/* The one place a format warning is true - see rule 3 in the file header. */
function cgExistingGroupNotice(rows, group, formatFor) {
  if (!group || !group.in_guide) return '';
  const lock = cgLockedFormat(group);
  if (!lock) return '';
  const differs = [];
  const unknown = [];
  rows.forEach((r) => {
    const f = formatFor(r);
    if (!f || !f.resolution || f.fps == null) { unknown.push(r); return; }
    if (f.resolution !== group.format_resolution
        || Math.round(f.fps) !== Math.round(group.format_fps)) differs.push(r);
  });
  if (!differs.length && !unknown.length) return '';
  let html = '<div class="notice notice-warn"><strong>'
    + `"${escHtml(group.name)}" is in the TV Guide and its format is locked to `
    + `${escHtml(lock)}.</strong><br>`;
  if (differs.length) {
    html += `${differs.map((r) => escHtml(r.channel_name)).join(', ')} - measured at a different `
      + 'format. They will join the group and stay switched on, but the lock skips them wherever '
      + 'a member is picked to serve the guide row or start a recording, until they match again.';
  }
  if (differs.length && unknown.length) html += '<br>';
  if (unknown.length) {
    html += `${unknown.map((r) => escHtml(r.channel_name)).join(', ')} - never health checked, so `
      + 'whether they match is unknown. Unknown is not skipped: the first health check '
      + 'measures them and the lock takes it from there.';
  }
  return `${html}</div>`;
}

/**
 * opts:
 *   channels    - [{channel_id, channel_name, account_color, account_name}] the picked
 *                 rows. Each one's measured format is fetched, not passed: the search's
 *                 row payload carries no resolution/fps and widening it for one modal
 *                 would put two joins on every row of a 136,130-channel search.
 *   introSeenVersion - the stored GROUP_INTRO_PREF_KEY value (0 when never dismissed),
 *                 server-rendered into the page so the first screen does not have to be
 *                 decided by a fetch after the modal is already up.
 *   checkOpts   - forwarded to openCreateCheckModal: the page's {profiles, profilesUrl,
 *                 testerBusy, scheduleTemplateId, schedulePrefix, windowSettingsUrl}.
 *   groupDetailUrlBase - '/channel-groups/' for the closing toast's link.
 *   onDone()    - called once the flow has finished. The default is deliberately a no-op:
 *                 the search page keeps its selection and does not reload.
 */
function openGroupCreateFlow(opts) {
  const picked = (opts.channels || []).slice();
  if (!picked.length) return null;
  const onDone = opts.onDone || (() => {});
  const detailBase = opts.groupDetailUrlBase || '/channel-groups/';
  let introSeen = Number(opts.introSeenVersion) || 0;
  const introNeeded = () => introSeen < GROUP_INTRO_VERSION;
  const groupLink = (id, name) => `<a href="${escHtml(detailBase + id)}">${escHtml(name)}</a>`;

  // Both fetched once and then reused across every screen and every ← Back: the existing
  // groups the picker offers (with the in_guide/lock data the one true warning is judged
  // against), and each picked channel's measured format. `formats` stays null until it
  // lands, which is not the same as "every channel is untested" - see cgPickedHtml.
  let groups = null;
  let formats = null;

  function loadFormats(then) {
    if (formats !== null) { then(); return; }
    jsonFetch(`/api/channel-tests/formats?channel_ids=${picked.map((r) => r.channel_id).join(',')}`)
      .then((data) => { formats = data.formats || {}; })
      .catch(() => { formats = {}; })
      .finally(then);
  }
  // A channel absent from the map has no measurement - unknown, which is never the same
  // claim as a format that differs.
  const formatOf = (row) => (formats ? formats[String(row.channel_id)] || null : null);

  function dismissIntro() {
    introSeen = GROUP_INTRO_VERSION;
    jsonFetch(`/api/user-prefs/${GROUP_INTRO_PREF_KEY}`, {
      method: 'POST',
      body: JSON.stringify({ value: GROUP_INTRO_VERSION }),
    }).catch(() => { /* best effort - a dismissal that did not stick just shows again */ });
  }

  /* ── Screen 1: what a channel group is ─────────────────────────────────────
     `carriedName`/`carriedMode` are screen 2's state coming BACK through here. */
  function openIntroScreen(carriedName, carriedMode) {
    const body = document.createElement('div');
    body.innerHTML =
      '<div class="notice notice-info cg-hero">The settings you pick for your channel group '
      + 'when you create it can always be changed later.</div>'
      + `<p class="cg-lead">You picked <strong>${picked.length} channel${
        picked.length === 1 ? '' : 's'}</strong>. A channel group keeps them together, and two `
      + 'things are built on top of that:</p>'
      + `<div class="cg-uses">${CG_USES.map((u) =>
        `<div class="cg-use"><strong>${escHtml(u.title)}</strong>`
        + `<span class="cg-sub">${escHtml(u.blurb)}</span></div>`).join('')}</div>`
      + '<p class="cg-next">Click Continue to proceed with creating the group.</p>'
      + '<label class="cg-dismiss"><input type="checkbox" id="cg-hide-intro">'
      + '<span>Do not show this page again</span></label>';

    // No Cancel: the X, the backdrop and Escape all already dismiss a modal, and a fourth
    // way to do nothing is not a footer's job.
    return buildModal({
      title: 'New channel group',
      panelClass: CG_PANEL_CLASS,
      body,
      footer: [
        { label: 'Continue', class: 'btn btn-primary',
          onClick: (close) => {
            if (body.querySelector('#cg-hide-intro').checked) dismissIntro();
            close();
            openGroupScreen(carriedName != null ? carriedName : cgPrefillName(picked), carriedMode);
            return false;
          } },
      ],
    });
  }

  /* ── Screen 2: the group ───────────────────────────────────────────────── */
  function openGroupScreen(initialName, initialMode) {
    let typed = initialName != null ? initialName : cgPrefillName(picked);
    let mode = initialMode || 'new';
    const cameFromIntro = introNeeded();
    const groupFor = (m) => (m === 'new' || !groups
      ? null : groups.find((g) => String(g.id) === String(m)));

    const body = document.createElement('div');
    body.innerHTML =
      '<div id="cg-error" class="notice notice-bad" style="display:none"></div>'
      + '<fieldset class="gd-fset"><div class="gd-fset-head">The group</div>'
      + fieldRow({
        label: 'Group', wide: true,
        meta: 'Start a new group from these channels, or add them to one you already have.',
        control: '<select id="cg-mode"><option value="new" selected>Create a new group</option></select>',
      })
      + fieldRow({
        id: 'name', label: 'Group name', wide: true,
        meta: 'How this group shows up in your groups list, in alerts and in notifications.',
        control: `<input type="text" id="cg-name" maxlength="255" value="${escHtml(typed)}"`
          + ' placeholder="Sports channels to watch">',
      })
      + fieldRow({
        full: true, label: `Channels in it (${picked.length})`,
        meta: `<div id="cg-picked-host">${cgPickedHtml(picked, formatOf, formats !== null)}</div>`,
      })
      + '</fieldset>'
      + '<div id="cg-notices"></div>'
      + '<div id="cg-warnings"></div>';

    const errEl = body.querySelector('#cg-error');
    const showErr = (msg) => { errEl.textContent = msg; errEl.style.display = ''; };

    function renderModeOptions() {
      const sel = body.querySelector('#cg-mode');
      sel.innerHTML = `<option value="new"${mode === 'new' ? ' selected' : ''}>Create a new group</option>`
        + (groups || []).map((g) =>
          `<option value="${g.id}"${String(g.id) === String(mode) ? ' selected' : ''}>`
          + `Add to "${escHtml(g.name)}" (${g.member_count})</option>`).join('');
    }

    function syncMode() {
      const isNew = mode === 'new';
      body.querySelector('[data-frow="name"]').style.display = isNew ? '' : 'none';
      // Rule 2 in the file header: a brand new group is warned about nothing.
      body.querySelector('#cg-notices').innerHTML = isNew
        ? '' : cgExistingGroupNotice(picked, groupFor(mode), formatOf);
      body.querySelector('#cg-warnings').innerHTML = '';
      // Adding to a group that already exists finishes here: there is nothing to create,
      // and that group's health check is managed on its own page.
      if (primary) primary.textContent = isNew ? 'Continue' : 'Add channels';
    }

    body.addEventListener('change', (e) => {
      if (e.target.id !== 'cg-mode') return;
      mode = e.target.value;
      syncMode();
    });
    // Every route out of this screen carries the typed name with it, so it is read off the
    // input as it changes rather than only when the input is about to be thrown away.
    body.addEventListener('input', (e) => { if (e.target.id === 'cg-name') typed = e.target.value; });

    const footer = [];
    if (cameFromIntro) {
      // The arrow is what marks this as navigation rather than an action (CLAUDE.md UI
      // naming). Nothing is behind this screen when the intro is dismissed, so it is not
      // drawn then.
      footer.push({ label: '← Back', class: 'btn',
        onClick: (close) => { close(); openIntroScreen(typed, mode); return false; } });
    }
    footer.push({ label: 'Continue', class: 'btn btn-primary',
      onClick: (close) => { go(close); return false; } });

    const modal = buildModal({
      title: 'New channel group', panelClass: CG_PANEL_CLASS, body, footer,
    });
    const primary = modal.querySelector('.modal-foot .btn-primary');
    syncMode();

    if (groups === null) {
      jsonFetch('/api/channel-groups').then((data) => {
        groups = data.groups || [];
      }).catch(() => { groups = []; })
        .finally(() => { renderModeOptions(); syncMode(); });
    } else {
      renderModeOptions();
    }
    // One updater per region: the list host is written here and by nothing else, and
    // syncMode() owns the notice below it - which has to be redrawn too, because the
    // buckets it sorts the selection into are exactly what just arrived.
    loadFormats(() => {
      const host = body.querySelector('#cg-picked-host');
      if (host) host.innerHTML = cgPickedHtml(picked, formatOf, true);
      syncMode();
    });

    /* Adding to a group that already exists is the only path here that writes anything,
       and it is the only one that can come back with the server's soft warnings - a mixed
       format or duplicate stream URLs among the would-be members. They render with a
       Proceed anyway, through the same builder group-modal.js draws them with. */
    function addToExisting(close, force) {
      const g = groupFor(mode);
      primary.disabled = true;
      errEl.style.display = 'none';
      jsonFetch(`/api/channel-groups/${mode}/members`, {
        method: 'POST',
        body: JSON.stringify({ channel_ids: picked.map((r) => Number(r.channel_id)), force }),
      }).then((data) => {
        if (!data.success) {
          const wrap = body.querySelector('#cg-warnings');
          wrap.innerHTML = groupWarningsHtml(data, 'cg-warn-force');
          wrap.querySelector('#cg-warn-force').addEventListener('click', () => {
            wrap.innerHTML = '';
            addToExisting(close, true);
          });
          return;
        }
        close();
        showToast(`Added ${picked.length} channel${picked.length === 1 ? '' : 's'} to `
          + `${groupLink(data.group_id, data.group_name || (g ? g.name : ''))}.`,
        { html: true, durationMs: CG_TOAST_MS });
        onDone();
      }).catch((err) => showErr(err.message || 'Request failed.'))
        .finally(() => { primary.disabled = false; });
    }

    function go(close) {
      if (mode !== 'new') { addToExisting(close, false); return; }
      const name = body.querySelector('#cg-name').value.trim();
      if (!name) { showErr('Give the group a name.'); return; }
      close();
      openCheckScreen(name, mode);
    }

    return modal;
  }

  /* ── Screen 3: check-modal.js, opened as the last step of a flow ────────────
     Nothing has been written yet. The group and its health check are created together by
     this screen's Create - see rule 1 in the file header. */
  function openCheckScreen(groupName, mode) {
    return openCreateCheckModal(Object.assign({}, opts.checkOpts, {
      channelIds: picked.map((r) => Number(r.channel_id)),
      createGroupName: groupName,
      memberCount: picked.length,
      modalTitle: 'New channel group',
      hasOwnCheck: false,
      inGuide: false,
      nameless: true,
      submitLabel: 'Create',
      panelClass: CG_PANEL_CLASS,
      onBack: () => openGroupScreen(groupName, mode),
      doneToast: (action, data) => {
        const link = groupLink(data.group_id, data.group_name || groupName);
        return (action === 'queue'
          ? `Group ${link} created with ${picked.length} channels. Its health check is `
            + 'not scheduled - start it whenever you want from the group\'s page.'
          : `Group ${link} created with ${picked.length} channels, and a health check `
            + `${action === 'start' ? 'is running now' : 'on the schedule you set'}.`);
      },
      // The selection deliberately survives: you are back where you were, with the same
      // rows ticked, free to group another set out of the same search.
      onDone,
    }));
  }

  return introNeeded() ? openIntroScreen() : openGroupScreen(cgPrefillName(picked));
}
