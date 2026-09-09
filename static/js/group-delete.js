/* Shared "Delete group" flow for the groups list (groups.js) and the group detail page
   (group-detail.js). Both pages offer the same destructive action, so both ask this.
   Depends on util.js (escHtml, jsonFetch, buildModal).

   One home rather than two copies because the interesting half is not the first dialog -
   it is what happens when the server refuses. Deleting a group takes
   DESIGN-channel-groups-model.md 15.1's split (dev/changelog/763): a capture under way
   refuses the delete outright, scheduled recordings are named and then cancelled once
   the user confirms. Nothing here decides any of that - the route refuses whether or not
   this code asks first, and the facts in each dialog come from the refusal itself, so
   neither page needs to know a group's recordings before the click. */

/**
 * opts:
 *   groupName      - for the dialog copy
 *   deleteUrl      - POST target for the group's delete route
 *   attachedChecks - [{name}, ...] health checks that go with the group (may be empty)
 *   recordingUrl(id) - optional; builds the link to a recording's detail page
 *   onDeleted(result) - called after the delete succeeds
 *   onError(message)  - called for a refusal this flow does not own
 */
function openDeleteGroupModal(opts) {
  const name = opts.groupName || 'this group';
  const checks = opts.attachedChecks || [];
  const recUrl = opts.recordingUrl || ((id) => `/recordings/${id}`);
  const onError = opts.onError || (() => {});
  const many = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  const note = (html) => `<p class="card-note">${html}</p>`;

  const confirmDialog = (o) => buildModal({
    title: o.title,
    body: o.lines.map(note).join(''),
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      { label: o.confirm, class: o.confirmClass || 'btn btn-danger',
        onClick: (c) => { c(); o.onConfirm(); return false; } },
    ],
  });

  // 15.1. A capture running right now is not something a delete may end. One way
  // forward, and it is the deliberate verb: go to the recording and abort it.
  const liveDialog = (info) => confirmDialog({
    title: 'This group is recording right now',
    lines: [
      `<strong>${escHtml(name)}</strong> is recording ${escHtml(info.name || '')} ` +
      `until ${escHtml(info.until || 'later')}.`,
      'Deleting the group would leave that recording with nowhere to fail over to. Let ' +
      'it finish, or abort it deliberately, and then come back to this.',
    ],
    confirm: 'Open the recording',
    confirmClass: 'btn btn-primary',
    onConfirm: () => { location.href = recUrl(info.recording_id); },
  });

  // Scheduled recordings cannot survive the group: a SCHEDULED row pointing at a group
  // that no longer exists fires later with no member to resolve. Named first, cancelled
  // only on the confirm.
  const scheduledDialog = (facts, retry) => {
    const listed = facts.scheduled || [];
    const count = facts.scheduled_count || listed.length;
    const lines = [
      `<strong>${escHtml(name)}</strong> has ${many(count, 'scheduled recording')} on it.`,
      'Deleting the group cancels ' + (count === 1 ? 'it' : 'them') + ' - the group is ' +
      'what each one records from, so there is nothing left to record.',
    ];
    if (listed.length) {
      lines.push('<ul class="card-note">' + listed.map(r =>
        `<li>${escHtml(r.name || 'Untitled')} - ${escHtml(r.start || '')}</li>`).join('') +
        (count > listed.length ? `<li>and ${many(count - listed.length, 'more')}</li>` : '') +
        '</ul>');
    }
    return confirmDialog({
      title: 'Cancel the scheduled recordings?',
      lines,
      confirm: `Delete group and cancel ${count === 1 ? 'it' : 'them'}`,
      onConfirm: retry,
    });
  };

  const post = (confirmed) => jsonFetch(opts.deleteUrl, {
    method: 'POST',
    body: confirmed ? JSON.stringify({ confirm: true }) : undefined,
  }).then(opts.onDeleted || (() => {})).catch(err => {
    const d = (err && err.data) || {};
    if (d.recording_in_progress) { liveDialog(d.recording_in_progress); return; }
    if (d.confirm_required) { scheduledDialog(d.confirm_required, () => post(true)); return; }
    onError(err.message || 'Delete failed.');
  });

  const cascade = checks.length > 0;
  const names = checks.map(c => `&ldquo;${escHtml(c.name)}&rdquo;`).join(', ');
  // Sentence 2 is the consequence, per DESIGN.md 4. It says the channels survive because
  // that is the question a dissolve raises - not that they get anything back: grouping
  // never took a guide row away (dev/changelog/751).
  const first = cascade
    ? `Group "${escHtml(name)}" will be deleted, along with the health check ` +
      `schedule${checks.length === 1 ? '' : 's'} it carries (${names}).`
    : `Group "${escHtml(name)}" will be deleted.`;
  return confirmDialog({
    title: 'Delete group',
    lines: [first,
            'Its channels are not deleted, and each keeps its own guide row if it has one.'],
    confirm: cascade
      ? `Delete group and health check${checks.length === 1 ? '' : 's'}`
      : 'Delete group',
    onConfirm: () => post(false),
  });
}
