/* The account actions both Accounts surfaces run (DESIGN.md §17).

   The list page's row kebab and the account page's action bar offer the same things, so
   they are implemented once here rather than twice: a sync, a cancel, a force-EPG resync
   and a delete, each going through the JSON API in app/routes/accounts.py. Two surfaces
   with their own copy of "sync this account" is how the two end up behaving differently
   after one of them is edited.

   The load-bearing piece is the conflict override. DESIGN-concurrency.md §5.4 says a
   manual sync that clashes with a recording or a test run is a WARNING THE USER MAY
   OVERRIDE, not a refusal: the API answers 409 with the reasons, and the caller shows them
   and offers to resubmit with `force`. The old form-POST list page did this with a
   `?sync_conflict=<id>` redirect and a "Sync Anyway" button; when the account page moved to
   the JSON API it showed the 409's message and offered no way past it, which is the defect
   this file also fixes (dev/docs/BUGS.md 2026-08-04).

   Callers pass `onDone` (usually a reload) and `onError` (a surface's own error line, on
   top of the toast every failure already gets).

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal).
*/

/* One reporting path for every action here: a toast always, plus the caller's own error
   surface if it has one. Never swallowed - an action that fails silently is the defect the
   Alerts page shipped with (dev/docs/BUGS.md 2026-08-03 @ 07:43:07 PM ET). */
function accountActionFailed(message, opts) {
  if (opts && opts.onError) opts.onError(message);
  showToast(message, { type: 'error' });
}

function accountActionDone(res, opts) {
  showToast((res && res.message) || 'Done.');
  if (opts && opts.onDone) opts.onDone(res);
  return res;
}

/* Start a sync. `force` overrides a concurrency conflict; `forceEpgResync` is an
   independent flag that bypasses the EPG collapse guard for this one run
   (DESIGN-sync-resilience.md §4) - the two mean different things and neither implies the
   other. */
function accountSync(id, opts = {}) {
  return jsonFetch(`/api/accounts/${id}/sync`, {
    method: 'POST',
    body: JSON.stringify({
      force: !!opts.force,
      force_epg_resync: !!opts.forceEpgResync,
    }),
  })
    .then((res) => accountActionDone(res, opts))
    .catch((e) => {
      const conflicts = (e.data && e.data.conflicts) || [];
      if (e.status === 409 && conflicts.length) {
        confirmSyncAnyway(id, conflicts, opts);
        return;
      }
      accountActionFailed(e.message || 'The sync could not be started.', opts);
    });
}

function confirmSyncAnyway(id, conflicts, opts) {
  buildModal({
    title: 'Something else is already running',
    body: '<p>This sync was not started, because it would overlap:</p><ul>' +
      conflicts.map((c) => `<li>${escHtml(c)}</li>`).join('') + '</ul>' +
      '<p class="text-muted small" style="margin-top:8px">Syncing anyway is allowed - it ' +
      'opens one brief connection to the provider - but it happens alongside whatever is ' +
      'listed above.</p>',
    footer: [
      { label: 'Leave it', class: 'btn', onClick: (c) => c() },
      {
        label: 'Sync anyway',
        class: 'btn btn-danger',
        onClick: (close) => {
          close();
          accountSync(id, Object.assign({}, opts, { force: true }));
          return false;
        },
      },
    ],
  });
}

function accountCancelSync(id, opts = {}) {
  return jsonFetch(`/api/accounts/${id}/sync/cancel`, { method: 'POST' })
    .then((res) => accountActionDone(res, opts))
    .catch((e) => accountActionFailed(e.message || 'The sync could not be cancelled.', opts));
}

function confirmForceEpgResync(id, opts = {}) {
  buildModal({
    title: 'Force EPG resync',
    body: '<p>Run a sync that imports this account\'s guide data <strong>even if it looks ' +
      'much smaller than last time</strong>?</p>' +
      '<p class="text-muted small" style="margin-top:8px">The collapse guard exists to stop ' +
      'a truncated provider feed wiping a good guide. This bypasses it for one run; ' +
      'everything else about the sync is normal.</p>',
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      {
        label: 'Force EPG resync',
        class: 'btn btn-primary',
        onClick: (close) => {
          close();
          accountSync(id, Object.assign({}, opts, { forceEpgResync: true }));
          return false;
        },
      },
    ],
  });
}

/* The confirm names what goes with the account, per DESIGN.md §4's confirm anatomy: the
   counts are what makes "delete this account" a decision rather than a guess. `syncs` is
   optional - the list row does not carry a sync count, and inventing one would be worse
   than leaving the clause out. */
function confirmDeleteAccount(opts = {}) {
  // Other accounts reading this one's EPG lose it too (DESIGN-epg-sources.md §9.5). The
  // confirm still opens if that lookup fails - it only drops the sentence.
  jsonFetch(`/api/accounts/${opts.id}/epg-readers`)
    .then((res) => buildDeleteAccountModal(opts, res.readers || []))
    .catch(() => buildDeleteAccountModal(opts, []));
}

function buildDeleteAccountModal(opts, readers) {
  const n = (v) => Number(v || 0).toLocaleString('en-US');
  const parts = [`${n(opts.channels)} channels`, `${n(opts.epg)} program entries`];
  if (opts.syncs !== undefined && opts.syncs !== null) parts.push(`${n(opts.syncs)} sync records`);
  const epg = readers.length
    ? '<p class="text-muted small" style="margin-top:8px">' +
      readers.map((r) => `<strong>${escHtml(r.name)}</strong> takes its guide for ${n(r.guided)} ` +
        `channel${r.guided === 1 ? '' : 's'} from this account's EPG`).join('; ') +
      '. Those channels will switch to their next source or lose their guide.</p>'
    : '';
  buildModal({
    title: 'Delete account',
    body: `<p>Delete <strong>${escHtml(opts.name || '')}</strong>?</p>` +
      '<p class="text-muted small" style="margin-top:8px">This removes its ' +
      `${parts.join(', ')}. Recordings already on disk are not deleted, but they lose the ` +
      `channel they came from.</p>${epg}`,
    footer: [
      { label: 'Cancel', class: 'btn', onClick: (c) => c() },
      {
        label: 'Delete account',
        class: 'btn btn-danger',
        onClick: (close) => {
          jsonFetch(`/api/accounts/${opts.id}`, { method: 'DELETE' })
            .then((res) => { close(); accountActionDone(res, opts); })
            .catch((e) => {
              close();
              accountActionFailed(e.message || 'The account could not be deleted.', opts);
            });
          return false;
        },
      },
    ],
  });
}
