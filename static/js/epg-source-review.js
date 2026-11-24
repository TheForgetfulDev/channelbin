/* Name-match review page (templates/epg_source_review.html, dev/changelog/1105).
   Every decision goes to the server, which checks it against the proposals it computes
   itself; the page is then re-rendered from the server rather than patched here. */
(() => {
  const main = () => document.getElementById('nm-main');
  const sourceId = () => main().dataset.sourceId;
  const api = (what) => `/api/epg-sources/${sourceId()}/name-matches/${what}`;

  const rowPick = (tr) => ({ channel_id: Number(tr.dataset.channelId), xml_id: tr.dataset.xmlId });
  const pickedRows = () => [...main().querySelectorAll('input[data-act="pick"]:checked')]
    .map((cb) => cb.closest('tr'));

  const syncBar = () => {
    const n = pickedRows().length;
    main().querySelectorAll('[data-act$="-selected"]').forEach((b) => { b.disabled = n === 0; });
  };

  const refreshPage = async () => {
    try {
      await swapFromServer(['#nm-main']);
    } catch (e) {
      showToast(`Could not reload the list: ${e.message}`, { type: 'error' });
    }
  };

  const send = async (btn, url, body) => {
    btn.disabled = true;
    try {
      const data = await jsonFetch(url, { method: 'POST', body: JSON.stringify(body) });
      showToast(data.message, { durationMs: 8000 });
      await refreshPage();
    } catch (e) {
      showToast(e.message, { type: 'error' });
      btn.disabled = false;
    }
  };

  // Stop using a shared id (dev/changelog/1716): the ids sent are exactly the channels the
  // row showed, and the server checks each against the finding as it stands.
  const ignoreShared = async (btn, tr) => {
    const ids = JSON.parse(tr.dataset.channelIds || '[]');
    const n = ids.length;
    const ok = await confirmModal({
      title: `Stop using ${tr.dataset.xmlId}`,
      message: `${n.toLocaleString()} channel${n === 1 ? '' : 's'} on ${tr.dataset.accountName} `
        + `stop matching the id ${tr.dataset.xmlId} in every guide that account reads.`,
      consequence: `The ${tr.dataset.guideName} schedule is removed from them now. A `
        + 'channel keeps a guide only from a source where it has a key of its own; name '
        + "matches can suggest one. A channel's own page can switch the provider's id back on.",
      confirmLabel: 'Stop using it',
      danger: true,
    });
    if (!ok) return;
    send(btn, `/api/epg-sources/${sourceId()}/shared-ids/ignore`, {
      account_id: Number(tr.dataset.accountId), xml_id: tr.dataset.xmlId, channel_ids: ids,
    });
  };

  // Accept all (dev/changelog/1718): every proposal the tab lists under its filters, not
  // only this page's. The list is read when the button is pressed, counted in the confirm,
  // and posted in chunks; the server checks each pick again, so a channel that stopped
  // being proposed in the meantime is counted and left alone.
  const ACCEPT_CHUNK = 500;
  const acceptAll = async (btn) => {
    const filters = new URL(location.href).searchParams;
    const query = new URLSearchParams();
    ['single', 'unguided'].forEach((k) => { if (filters.get(k) === '1') query.set(k, '1'); });
    if (filters.get('account')) query.set('account', filters.get('account'));
    let picks;
    btn.disabled = true;
    try {
      picks = (await jsonFetch(`/api/epg-sources/${sourceId()}/name-matches?${query}`)).picks;
    } catch (e) {
      showToast(e.message, { type: 'error' });
      btn.disabled = false;
      return;
    }
    const n = picks.length;
    if (!n) {
      showToast('No name matches are proposed any more.');
      await refreshPage();
      return;
    }
    const ok = await confirmModal({
      title: 'Accept all name matches',
      message: `${n.toLocaleString()} channel${n === 1 ? '' : 's'} take the matched id as `
        + `${n === 1 ? 'its' : 'their'} key for ${main().dataset.sourceName}.`,
      confirmLabel: `Accept ${n.toLocaleString()}`,
    });
    if (!ok) {
      btn.disabled = false;
      return;
    }
    const label = btn.textContent;
    const sum = { accepted: 0, waiting: 0, stale: 0 };
    let failed = null;
    for (let i = 0; i < n; i += ACCEPT_CHUNK) {
      btn.textContent = `Accepting ${Math.min(i + ACCEPT_CHUNK, n).toLocaleString()} of ${n.toLocaleString()}...`;
      try {
        const data = await jsonFetch(api('accept'), {
          method: 'POST', body: JSON.stringify({ picks: picks.slice(i, i + ACCEPT_CHUNK) }),
        });
        sum.accepted += data.accepted;
        sum.waiting += data.waiting;
        sum.stale += data.stale;
      } catch (e) {
        failed = e.message;
        break;
      }
    }
    const parts = [`Accepted ${sum.accepted.toLocaleString()} name match${sum.accepted === 1 ? '' : 'es'}.`];
    if (sum.waiting) parts.push(`${sum.waiting.toLocaleString()} get their listings at the source's next refresh.`);
    if (sum.stale) parts.push(`${sum.stale.toLocaleString()} were no longer proposed and were left alone.`);
    if (failed) parts.push(`Stopped before the rest: ${failed}`);
    showToast(parts.join(' '), { type: failed ? 'error' : undefined, durationMs: 10000 });
    btn.textContent = label;
    await refreshPage();
  };

  document.addEventListener('change', (ev) => {
    const el = ev.target;
    if (!main() || !main().contains(el)) return;
    if (el.dataset.act === 'select-all') {
      main().querySelectorAll('input[data-act="pick"]').forEach((cb) => { cb.checked = el.checked; });
      syncBar();
    } else if (el.dataset.act === 'pick') {
      syncBar();
    } else if (el.dataset.filter) {
      const url = new URL(location.href);
      if (el.checked) url.searchParams.set(el.dataset.filter, '1');
      else url.searchParams.delete(el.dataset.filter);
      url.searchParams.delete('page');
      location.href = url.toString();  // nav-ok: a filter is a new page of results, like a link
    }
  });

  document.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (!btn || !main() || !main().contains(btn)) return;
    const tr = btn.closest('tr');
    switch (btn.dataset.act) {
      case 'accept':
        send(btn, api('accept'), { picks: [rowPick(tr)] });
        break;
      case 'reject':
        send(btn, api('reject'), { picks: [rowPick(tr)] });
        break;
      case 'accept-all':
        acceptAll(btn);
        break;
      case 'accept-selected':
        send(btn, api('accept'), { picks: pickedRows().map(rowPick) });
        break;
      case 'reject-selected':
        send(btn, api('reject'), { picks: pickedRows().map(rowPick) });
        break;
      case 'again':
        send(btn, api('propose-again'), { channel_ids: [Number(tr.dataset.channelId)] });
        break;
      case 'again-selected':
        send(btn, api('propose-again'),
             { channel_ids: pickedRows().map((r) => Number(r.dataset.channelId)) });
        break;
      case 'ignore-shared':
        ignoreShared(btn, tr);
        break;
      case 'refresh':
        send(btn, `/api/accounts/${main().dataset.ownerId}/epg-sources/${sourceId()}/refresh`, {});
        break;
      default:
        break;
    }
  });
})();
