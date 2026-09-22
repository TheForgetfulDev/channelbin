/* Unified "Clone" action - the one entry point every Clone kebab item calls
   (dev/changelog/542). Replaces three previously separate, mutually inconsistent
   behaviors: the list page's channel-and-settings-blind clone, the earlier
   "Clone health check..." action (no type picker), and the channel group detail
   page's broken bare clone (POST with no body, always 400s).

   There is one screen. It used to open on a scope picker - group and health check /
   group only / health check only - which stopped meaning anything once every group came
   to carry exactly one check that is minted with it and dies with it: two of the three
   answers described objects that cannot exist apart (dev/changelog/1077). What is left
   is a single question about how much of the source to carry over, and that is a
   checkbox on the group screen rather than a screen of its own (dev/changelog/1078).

   openCreateGroupModal (create-group-modal.js) is that screen. It posts
   POST /api/channel-groups/<src>/clone, which mints the copy's own check; this file
   supplies the `copySchedule` option that decides whether the source check's profile and
   schedule travel with it. The server carries a schedule over only when the source's is
   live, and reports back what it actually did (`schedule_copied`, `profile_copied`) -
   the checkbox is a request, not a promise.

   Depends on util.js (jsonFetch, showToast) and create-group-modal.js.

   opts (page-level globals every caller already has):
     existingNames, resolutionOptions, fpsOptions - forwarded to openCreateGroupModal
     onDone() - called once the clone is done
*/
function openCloneModal(groupId, opts) {
  jsonFetch(`/api/channel-groups/${groupId}/clone-info`)
    .then((src) => openCloneGroupScreen(src, opts))
    .catch((err) => showToast(err.message, { type: 'error' }));
}

function openCloneGroupScreen(src, opts) {
  const onDone = opts.onDone || (() => window.location.reload());
  const check = src.check || null;
  return openCreateGroupModal({
    srcId: src.id,
    srcName: src.name,
    jobId: check ? check.job_id : null,
    channels: src.channels || [],
    existingNames: opts.existingNames,
    resolutionOptions: opts.resolutionOptions,
    fpsOptions: opts.fpsOptions,
    defaultName: `${src.name} (copy)`,
    initialSettings: src.channel_settings,
    // Offered only when there is a live schedule to copy: a check whose schedule was
    // removed or paused has nothing the clone route would carry over, so a checkbox
    // there would be a control that does nothing whichever way it is set.
    copySchedule: !!(check && check.schedule_live),
    onDone,
  });
}
