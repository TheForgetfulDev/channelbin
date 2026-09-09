/* Unified "Clone" action - the one entry point every Clone kebab item calls
   (dev/changelog/542). Replaces three previously separate, mutually inconsistent
   behaviors: the list page's channel-and-settings-blind clone, the earlier
   "Clone health check..." action (no type picker), and the channel group detail
   page's broken bare clone (POST with no body, always 400s).

   Step 1 here (openCloneTypeModal) is ONLY a scope picker - name and channel selection
   each belong to whichever target-specific screen comes next, so neither is asked for
   twice. There is one kind of group now, so the question is no longer "which kind" but
   how much of a group carrying a schedule to copy:
     - No schedule attached: nothing to ask, so step 1 is skipped entirely.
     - A schedule attached (clone-info's `check` is present): Both (default) / Group only
       / Health check only.

   Step 2 (opened after Continue, step 1 already closed) reuses the existing
   target-specific screens as-is, seeded from GET .../clone-info:
     target=group       -> openCreateGroupModal (create-group-modal.js)
     target=schedule_only  -> openCloneCheckModal (clone-check-modal.js)
     target=both        -> openCreateGroupModal first; on success (its own onDone is
                            overridden here, not the default "navigate to the new
                            group"), open openCreateCheckModal in attach mode
                            (groupId=<new group>) so it uses the new group's own
                            membership automatically - no second channel picker, per
                            Groups unification. That modal's onDone AND onCancel both
                            resolve to the original opts.onDone: the channel-group half
                            already exists once step 2 succeeds, so the page needs to
                            reflect that whether or not the user finishes attaching a
                            check.

   Depends on util.js (jsonFetch, showToast, buildModal, escHtml), create-group-modal.js,
   clone-check-modal.js, check-modal.js.

   opts (page-level globals every caller already has):
     existingNames, resolutionOptions, fpsOptions - forwarded to openCreateGroupModal
     profiles, profilesUrl, testerBusy,
     scheduleTemplateId, schedulePrefix - forwarded to the check-target screens
     onDone() - called once the whole clone (all steps) is done
*/
function openCloneModal(groupId, opts) {
  jsonFetch(`/api/channel-groups/${groupId}/clone-info`)
    .then((src) => openCloneTypeModal(src, opts))
    .catch((err) => showToast(err.message, { type: 'error' }));
}

function openCloneTypeModal(src, opts) {
  const onDone = opts.onDone || (() => window.location.reload());
  const linked = !!src.has_schedule;
  // Nothing to choose between when the source carries no schedule - go straight to the
  // group screen rather than showing a one-option picker.
  if (!linked) { openCloneStep2(src, 'group', opts, onDone); return null; }
  const state = { target: 'both' };

  const body = document.createElement('div');
  let continueBtn = null;

  function typeOpt(value, title, meta) {
    const sel = state.target === value ? ' sel' : '';
    return `<label class="clone-type-opt${sel}">` +
      `<input type="radio" name="clone-target" value="${value}"${state.target === value ? ' checked' : ''}> ` +
      `<strong>${escHtml(title)}</strong><br><span class="text-muted small">${escHtml(meta)}</span></label>`;
  }

  function render() {
    const options =
      typeOpt('both', 'Group and health check',
        'A new group and a new health check attached to it - matching what is being cloned.') +
      typeOpt('group', 'Group only', 'Just the group and its channels. No health check attached.') +
      typeOpt('schedule_only', 'Health check only',
        'Just the health check, as an independent, unattached check.');

    body.innerHTML =
      `<div class="notice notice-info"><strong>"${escHtml(src.name)}" is not touched</strong> - ` +
      'this creates an independent copy.</div>' +
      `<div class="clone-type-choice">${options}</div>`;
  }

  body.addEventListener('change', (e) => {
    if (e.target.name === 'clone-target') { state.target = e.target.value; render(); }
  });

  const modal = buildModal({
    title: `Clone "${src.name}"`,
    body,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: 'Continue', class: 'btn btn-primary', onClick: (close) => {
        close();
        openCloneStep2(src, state.target, opts, onDone);
        return false;
      } },
    ],
  });
  continueBtn = modal.querySelector('.modal-foot .btn-primary');
  render();
  return modal;
}

function openCloneStep2(src, target, opts, onDone) {
  if (target === 'schedule_only') {
    openCloneCheckModal({
      jobId: src.check ? src.check.job_id : null,
      jobName: src.name,
      channels: src.channels || [],
      profileId: src.check ? src.check.profile_id : null,
      profiles: opts.profiles,
      profilesUrl: opts.profilesUrl,
      testerBusy: opts.testerBusy,
      windowSettingsUrl: opts.windowSettingsUrl,
      schedule: src.check ? src.check.schedule : null,
      scheduleTemplateId: opts.scheduleTemplateId,
      schedulePrefix: opts.schedulePrefix,
      onDone,
    });
    return;
  }

  // target === 'group' or 'both' - both start with the group screen.
  const groupDone = target === 'both'
    ? (resp) => {
        openCreateCheckModal({
          groupId: resp.group_id,
          groupName: resp.group_name,
          // Best estimate, not authoritative: the group's real membership may be
          // smaller if the previous screen's picker removed channels. Cosmetic only -
          // the submit itself always resolves against the group's actual membership
          // server-side (attach_group_id), never this count.
          memberCount: (src.channels || []).length,
          profiles: opts.profiles,
          profilesUrl: opts.profilesUrl,
          testerBusy: opts.testerBusy,
          windowSettingsUrl: opts.windowSettingsUrl,
          scheduleTemplateId: opts.scheduleTemplateId,
          schedulePrefix: opts.schedulePrefix,
          inGuide: false,
          hasOwnCheck: false,
          initialProfileId: src.check ? src.check.profile_id : null,
          initialSchedule: src.check ? src.check.schedule : null,
          onDone,
          onCancel: onDone,
        });
      }
    : onDone;

  openCreateGroupModal({
    srcId: src.id,
    srcName: src.name,
    jobId: src.check ? src.check.job_id : null,
    channels: src.channels || [],
    existingNames: opts.existingNames,
    resolutionOptions: opts.resolutionOptions,
    fpsOptions: opts.fpsOptions,
    defaultName: `${src.name} (copy)`,
    initialSettings: src.channel_settings,
    onDone: groupDone,
  });
}
