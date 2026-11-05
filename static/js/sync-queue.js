/* Starting the sync queue (app/sync_queue.py), for every button that syncs several accounts:
   the Readiness check's "Sync N accounts now" and the Accounts list's Sync All.

   The server takes the whole list in one request and works through it, so the page can be left.
   One request per account cannot work: a sync refuses to overlap another, so the first account
   synced and the second answered an error. The confirm says what is about to happen, and names
   anything that would hold it back so the user can wait it out or go ahead.

   `accounts` is who to sync, as [{id, name}]; left out, it is every account, read from the
   server at the click. A single account from Readiness starts at once because it has no order
   to explain and no pause to sit through; `alwaysConfirm` asks anyway.

   Depends on util.js (buildModal, escHtml, fmtDur, jsonFetch, plural, showToast). */

function startSyncQueue({ url, label, accounts, override, onStart, onSettled }) {
  onStart();
  return jsonFetch(url, {
    method: 'POST', body: JSON.stringify({ account_ids: accounts.map((a) => a.id), override }),
  }).then((res) => {
    showToast(res.message);
  }).catch((e) => {
    showToast(`${label} failed: ${e.message}`, { type: 'error' });
  }).finally(onSettled);
}

function showRunningSyncQueue({ url, sentence, onSettled }) {
  buildModal({
    title: 'Accounts are syncing',
    body: `<p>${escHtml(sentence)}.</p>`,
    footer: [
      { label: 'Close', class: 'btn', onClick: (close) => close() },
      {
        label: 'Stop syncing the rest',
        class: 'btn btn-danger',
        onClick: (close) => {
          close();
          jsonFetch(`${url}/stop`, { method: 'POST' })
            .then((res) => showToast(res.message))
            .catch((e) => showToast(`Could not stop that: ${e.message}`, { type: 'error' }))
            .finally(onSettled);
          return false;
        },
      },
    ],
  });
}

function confirmSyncQueue({
  url, label, accounts = null, alwaysConfirm = false,
  onStart = () => {}, onSettled = () => {},
}) {
  jsonFetch(url).then((info) => {
    if (info.queue) {
      showRunningSyncQueue({ url, sentence: info.sentence, onSettled });
      return;
    }
    const list = accounts || info.accounts;
    if (!list.length) {
      showToast('There are no accounts to sync.');
      return;
    }
    const conflicts = info.conflicts || [];
    const start = (override) => startSyncQueue({
      url, label, accounts: list, override, onStart, onSettled,
    });
    if (list.length === 1 && !conflicts.length && !alwaysConfirm) {
      start(false);
      return;
    }
    const pause = fmtDur(info.pause_seconds, false);
    const lead = list.length === 1
      ? `<p>Fetch the channel list and guide for <strong>${escHtml(list[0].name)}</strong>.</p>`
      : `<p>Accounts sync one at a time, so these run in turn, with a pause of ${escHtml(pause)}
         between each, until all ${list.length} are done:</p>
         <ul>${list.map((a) => `<li>${escHtml(a.name)}</li>`).join('')}</ul>
         <p class="text-muted small">This keeps going if you leave this page. An account that
         fails does not stop the others.</p>`;
    const held = conflicts.length
      ? `<p>This would be held back right now:</p>
         <ul>${conflicts.map((c) => `<li>${escHtml(c)}</li>`).join('')}</ul>
         <p class="text-muted small">Wait, and it starts once that clears. Sync anyway, and it
         runs alongside it.</p>`
      : '';
    const go = (override) => (close) => {
      close();
      start(override);
      return false;
    };
    buildModal({
      title: label,
      body: lead + held,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (close) => close() },
        ...(conflicts.length
          ? [{ label: 'Wait, then sync', class: 'btn btn-primary', onClick: go(false) },
             { label: 'Sync anyway', class: 'btn btn-danger', onClick: go(true) }]
          : [{
            label: `Sync ${list.length} ${plural(list.length, 'account')}`,
            class: 'btn btn-primary',
            onClick: go(false),
          }]),
      ],
    });
  }).catch((e) => {
    showToast(`Could not start that: ${e.message}`, { type: 'error' });
  });
}
