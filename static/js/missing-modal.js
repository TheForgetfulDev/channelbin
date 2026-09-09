/* Shared "Delete Missing Channels" modal - channels no longer seen in a provider's synced
   feed for a while (channel_lifecycle_state() 'missing'). Call sites:
   static/js/channel-search.js (Browse tab, database-wide AND the selection delete),
   static/js/group-detail.js (this group/health check's own channels, dev/changelog/653)
   and static/js/channel-detail.js (this one channel, dev/changelog/772).

   The preview is always fetched fresh, never trusted from a page-load snapshot - this is
   the one irreversible action on any of these surfaces. It is also what makes the modal a
   legitimate confirm step for a single channel: when nothing in the requested set is
   eligible, the preview says which reason applies and no delete button is built, so the
   refusal IS the dialog rather than an error after a click. Depends on util.js (jsonFetch,
   escHtml, buildModal, showToast, nf). */

/**
 * opts:
 *   title      - optional heading override (default 'Delete Missing Channels')
 *   scopeText  - describes what's being scanned, e.g. 'across all accounts' /
 *                 'in this group' / 'in this health check' (blank is fine)
 *   previewUrl - GET, returns {buckets, eligible_count, eligible_hidden_count,
 *                 blocked_inuse_count, blocked_inuse, blocked_active_count, blocked_active,
 *                 not_missing_count, not_missing}
 *   deleteUrl  - POST target for the actual delete
 *   deleteBody - object to JSON-stringify as the delete POST body
 *   emptyText  - the lead sentence when nothing is eligible, i.e. when this dialog is a
 *                 refusal rather than a confirm. Default 'Nothing here can be deleted
 *                 right now.'; a scoped caller can name what it asked about instead
 *   onDone(result) - called after a successful delete instead of the default
 *                 location.reload() (e.g. a scoped caller can just refresh its own rows)
 */
async function openMissingModal(opts) {
  const body = document.createElement('div');
  body.innerHTML = '<p class="text-muted small">Loading…</p>';
  const modal = buildModal({ title: opts.title || 'Delete Missing Channels', body,
                             footer: [{ label: 'Cancel', class: 'btn', onClick: (c) => c() }] });
  let data;
  try {
    data = await jsonFetch(opts.previewUrl);
  } catch (err) {
    body.innerHTML = `<p class="notice notice-bad">${escHtml(err.message || 'Failed to load preview.')}</p>`;
    return;
  }

  function blockedListHtml(items, total) {
    if (!items.length) return '';
    const rows = items.map((c) => `<li><a href="${escHtml(c.url)}">${escHtml(c.name)}</a></li>`).join('');
    const more = total > items.length
      ? `<li class="text-muted small">+ ${nf(total - items.length)} more</li>` : '';
    return `<ul class="rec-block-list">${rows}${more}</ul>`;
  }

  const buckets = data.buckets.filter((b) => b.count > 0)
    .map((b) => `<li>${nf(b.count)} missing ${escHtml(b.label)}</li>`).join('') || '<li>None</li>';
  let warn = '';
  if (data.blocked_inuse_count) {
    warn += `<div class="notice notice-warn"><strong>${nf(data.blocked_inuse_count)}</strong> `
      + 'excluded - in the guide or in a group (visit each to remove first):'
      + `${blockedListHtml(data.blocked_inuse, data.blocked_inuse_count)}</div>`;
  }
  if (data.blocked_active_count) {
    warn += `<div class="notice notice-warn"><strong>${nf(data.blocked_active_count)}</strong> `
      + 'excluded - has a recording scheduled or in progress:'
      + `${blockedListHtml(data.blocked_active, data.blocked_active_count)}</div>`;
  }
  /* Only the explicitly-requested scopes (one channel, or a selection) can produce this:
     an id that is neither eligible nor blocked is still listed by its provider, so it is
     not a missing channel at all. Saying so beats letting it vanish from the arithmetic. */
  if (data.not_missing_count) {
    warn += `<div class="notice notice-warn"><strong>${nf(data.not_missing_count)}</strong> `
      + "excluded - still listed in the provider's feed:"
      + `${blockedListHtml(data.not_missing || [], data.not_missing_count)}</div>`;
  }
  const n = data.eligible_count;
  /* The reassuring "nothing is in use" note belongs to a batch that HAS something in it.
     With nothing eligible and nothing blocked there is simply nothing here at all, and
     the note would answer a question the reader did not ask. */
  if (!warn && n) warn = '<div class="notice notice-ok">No channels in this batch are in use.</div>';
  const scope = opts.scopeText ? `${opts.scopeText} ` : '';
  /* Nothing eligible means no delete button is coming, so the lead sentence must not
     promise one. "This will permanently delete 0 channels" reads as a broken dialog on
     the single-channel surface, where zero eligible is the ordinary refusal rather than
     an empty sweep. The reasons are already in `warn` either way. */
  /* Hidden channels stay eligible for this sweep - hidden means out of the way, not
     protected - so the batch is named honestly rather than letting a delete count include
     channels nobody could currently see in the search with no way to tell. */
  const hiddenNote = data.eligible_hidden_count
    ? ` (${nf(data.eligible_hidden_count)} already hidden)` : '';
  const lead = n
    ? `<p>This will permanently delete <strong>${nf(n)}</strong> channel${n === 1 ? '' : 's'}${hiddenNote} `
      + `${scope}that ${n === 1 ? 'has' : 'have'} not appeared in a sync in a while:</p>`
      + `<ul>${buckets}</ul>`
    : `<p>${escHtml(opts.emptyText || 'Nothing here can be deleted right now.')}</p>`;
  body.innerHTML = lead + warn
    + '<p class="text-muted small">Completed/failed/aborted recordings from these channels are kept '
    + '(name-only); scheduled or in-progress recordings block that channel from this batch entirely.</p>';
  const foot = modal.querySelector('.modal-foot');
  if (!foot || !n) return;
  const del = document.createElement('button');
  del.className = 'btn btn-danger';
  const idleText = `Delete ${nf(n)} channel${n === 1 ? '' : 's'}`;
  del.textContent = idleText;
  del.addEventListener('click', () => {
    del.disabled = true;
    /* The account/duplicate-stream-url counters this recomputes are whole-table passes
       that run regardless of scope size (dev/changelog/684/685), so even a handful of
       channels can take a second or two - this is the honest-progress answer to that,
       not a fix for it (CLAUDE.md product principle 1). */
    del.textContent = 'Deleting…';
    jsonFetch(opts.deleteUrl, { method: 'POST', body: JSON.stringify(opts.deleteBody || {}) })
      .then((res) => {
        /* Close now rather than after the toast delay below: a caller whose onDone does
           an in-place refresh instead of navigating away (group-detail.js) has nothing
           else that would ever dismiss this dialog. */
        modal.closeModal();
        /* The skipped count is named here as well as in the dialog: on the selection
           surface the dialog is gone by now and a bare "Deleted 7" against 10 selected
           rows reads as a bug rather than as the exclusions the user was just shown. */
        const skipped = (res.blocked_inuse_count || 0) + (res.blocked_active_count || 0)
          + (res.not_missing_count || 0);
        showToast(`Deleted ${nf(res.deleted_count)} channel${res.deleted_count === 1 ? '' : 's'}`
          + (res.recordings_preserved
            ? ` (${nf(res.recordings_preserved)} recording${res.recordings_preserved === 1 ? '' : 's'} kept, name-only)`
            : '')
          + (skipped ? `; ${nf(skipped)} skipped.` : '.'));
        setTimeout(() => { if (opts.onDone) opts.onDone(res); else location.reload(); }, 900);
      })
      .catch((err) => {
        showToast(err.message || 'Delete failed.', { type: 'error' });
        del.disabled = false;
        del.textContent = idleText;
      });
  });
  foot.appendChild(del);
}
