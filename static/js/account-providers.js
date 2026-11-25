/* The Accounts page's Providers section (DESIGN-account-providers.md §6.2,
   dev/changelog/1170): create, rename and delete a provider, put an account on one or take
   it off, share a login with another account on the same provider or stop sharing it, and
   keep the provider's EPG order and its own guides (DESIGN-epg-sources.md §15.4).

   Every action is one request to /api/providers* or /api/logins/<id>/share, then a
   server-rendered swap of #acct-providers - the section has exactly one renderer, the
   template. Handlers are delegated from the document, so a swapped section needs no
   rebinding. On a phone a kebab or a login chip opens a bottom sheet holding the same
   menu items, as the account rows' kebab does.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, confirmModal, fieldRow,
   closeMenus, swapFromServer). */
(() => {
  const section = () => document.getElementById('acct-providers');

  function send(url, opts = {}) {
    return jsonFetch(url, opts)
      .then((res) => {
        showToast(res.message || 'Done.');
        return swapFromServer(['#acct-providers']);
      })
      .catch((e) => {
        showToast(e.message || 'Request failed.', { type: 'error' });
        throw e;
      });
  }

  const readJson = (el, key) => {
    try { return JSON.parse(el.dataset[key] || '[]'); } catch (e) { return []; }
  };

  // ── Dialogs ──────────────────────────────────────────────────────────────
  function accountChecks(choices, providerName) {
    return choices.map((c) => {
      const note = c.provider && c.provider !== providerName
        ? ` <span class="text-faint">(on ${escHtml(c.provider)} now, would move)</span>` : '';
      return `<label class="check-row"><input type="checkbox" value="${c.id}"> ` +
        `${escHtml(c.name)}${note}</label>`;
    }).join('');
  }

  function openAddProvider() {
    const choices = readJson(section(), 'choices');
    const body = document.createElement('div');
    body.innerHTML =
      fieldRow({
        label: 'Name', stack: true,
        meta: 'What you call the service behind these accounts.',
        control: '<input type="text" id="prov-name" autocomplete="off" maxlength="100">',
      }) +
      fieldRow({
        label: 'Accounts', stack: true,
        meta: 'Only accounts that reach the same backend - the same stream ids are the same ' +
          'channels on all of them. ChannelBin never decides this for you.',
        control: `<div id="prov-accts">${accountChecks(choices, null)}</div>`,
      });
    buildModal({
      title: 'Add provider',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: 'Add provider',
          class: 'btn btn-primary',
          onClick: (close) => {
            const name = body.querySelector('#prov-name').value.trim();
            const ids = Array.from(body.querySelectorAll('#prov-accts input:checked'))
              .map((i) => Number(i.value));
            if (!name) { showToast('Give the provider a name.', { type: 'error' }); return false; }
            send('/api/providers', { method: 'POST', body: JSON.stringify({ name, account_ids: ids }) })
              .then(() => close()).catch(() => {});
            return false;
          },
        },
      ],
    });
    setTimeout(() => body.querySelector('#prov-name').focus(), 50);
  }

  function openRename(card) {
    const body = document.createElement('div');
    body.innerHTML = fieldRow({
      label: 'Name', stack: true,
      control: '<input type="text" id="prov-rename" autocomplete="off" maxlength="100">',
    });
    body.querySelector('#prov-rename').value = card.dataset.name || '';
    buildModal({
      title: 'Rename provider',
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: 'Save',
          class: 'btn btn-primary',
          onClick: (close) => {
            const name = body.querySelector('#prov-rename').value.trim();
            if (!name) { showToast('Give the provider a name.', { type: 'error' }); return false; }
            send(`/api/providers/${card.dataset.providerId}/rename`,
              { method: 'POST', body: JSON.stringify({ name }) })
              .then(() => close()).catch(() => {});
            return false;
          },
        },
      ],
    });
    setTimeout(() => body.querySelector('#prov-rename').select(), 50);
  }

  function openAddAccount(card) {
    const addable = readJson(card, 'addable');
    const body = document.createElement('div');
    body.innerHTML = fieldRow({
      label: 'Account', stack: true,
      meta: 'An account moving from another provider stops sharing the logins it shared there.',
      control: '<select id="prov-acct" class="form-control">' +
        addable.map((c) => `<option value="${c.id}">${escHtml(c.name)}` +
          `${c.provider ? ` (on ${escHtml(c.provider)} now)` : ''}</option>`).join('') +
        '</select>',
    });
    buildModal({
      title: `Add account to ${card.dataset.name}`,
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: 'Add account',
          class: 'btn btn-primary',
          onClick: (close) => {
            const accountId = Number(body.querySelector('#prov-acct').value);
            send(`/api/providers/${card.dataset.providerId}/accounts`,
              { method: 'POST', body: JSON.stringify({ account_id: accountId }) })
              .then(() => close()).catch(() => {});
            return false;
          },
        },
      ],
    });
  }

  function confirmRemoveAccount(card, el) {
    confirmModal({
      title: 'Remove account from provider',
      message: `Take ${el.dataset.account} off ${card.dataset.name}?`,
      consequence: 'It stops sharing any login another account on the provider still holds. ' +
        'Logins only it holds stay on it.',
      confirmLabel: 'Remove',
    }).then((ok) => {
      if (!ok) return;
      send(`/api/providers/${card.dataset.providerId}/accounts/${el.dataset.accountId}`,
        { method: 'DELETE' }).catch(() => {});
    });
  }

  // A provider that owns EPG sources asks what becomes of them, on by default and never
  // assumed (DESIGN-epg-sources.md §15.5): kept, each goes to the lowest-numbered account
  // reading it and no guide changes; turned off, they are deleted with the provider.
  function confirmDelete(card) {
    jsonFetch(`/api/providers/${card.dataset.providerId}/delete-preview`)
      .catch(() => ({ sources: 0 }))
      .then((pre) => {
        const body = document.createElement('div');
        const n = pre.sources || 0;
        body.innerHTML =
          `<p>Accounts on ${escHtml(card.dataset.name)} will be unlinked.</p>` +
          '<p class="text-muted small" style="margin-top:8px">Their channels will stop folding ' +
          'together. Their hosts and logins stay as they are, and a login two of them share ' +
          'stays shared until you change it.</p>' +
          (n ? '<label class="check-row" style="margin-top:10px"><input type="checkbox" id="prov-keep" checked> ' +
            `Keep its ${n} EPG source${n === 1 ? '' : 's'} for the accounts that use them</label>` +
            '<p class="text-muted small" id="prov-keep-note"></p>' : '');
        const note = () => {
          const el = body.querySelector('#prov-keep-note');
          if (!el) return;
          const keep = body.querySelector('#prov-keep').checked;
          const g = pre.guided || 0;
          el.textContent = keep
            ? 'Each goes to the first account reading it, and every account keeps reading it, so no guide changes.' +
              (pre.unread ? ` ${pre.unread} that no account reads ${pre.unread === 1 ? 'is' : 'are'} deleted.` : '')
            : `They are deleted. ${g ? `${g.toLocaleString()} channel${g === 1 ? ' takes its' : 's take their'} guide from them now, and each switches to its next source or has no guide.` : 'No channel takes its guide from them now.'}`;
        };
        const keepBox = body.querySelector('#prov-keep');
        if (keepBox) { keepBox.addEventListener('change', note); note(); }
        buildModal({
          title: 'Delete provider',
          body,
          footer: [
            { label: 'Cancel', class: 'btn', onClick: (c) => c() },
            {
              label: 'Delete provider',
              class: 'btn btn-danger',
              onClick: (close) => {
                const payload = keepBox ? { keep_sources: keepBox.checked } : {};
                send(`/api/providers/${card.dataset.providerId}`,
                  { method: 'DELETE', body: JSON.stringify(payload) })
                  .then(() => close()).catch(() => {});
                return false;
              },
            },
          ],
        });
      });
  }

  // ── The provider's EPG list (DESIGN-epg-sources.md §15.4) ──────────────────
  const epgUrl = (card, id) =>
    `/api/providers/${card.dataset.providerId}/epg-sources${id ? `/${id}` : ''}`;

  // duplicated from static/js/account-detail.js openSourceModal - this dialog is on another
  // page, always takes an interval (a provider's source has no account sync to ride) and
  // posts to the provider's endpoint.
  function openEpgSource(card, src) {
    const editing = !!src;
    const s = src || { name: '', url: '', refresh_interval_hours: 24, enabled: true };
    const hours = readJson(section(), 'sourceHours').map((h) =>
      `<option value="${h}"${String(s.refresh_interval_hours) === String(h) ? ' selected' : ''}>` +
      `Every ${h} hour${h === 1 ? '' : 's'}</option>`).join('');
    const body = document.createElement('div');
    body.innerHTML =
      fieldRow({
        label: 'XMLTV URL', stack: true,
        meta: `A TV guide file, plain or <code>.gz</code>, read by every account on ${escHtml(card.dataset.name)}. ` +
          'Kept secret like an account\'s own URLs.',
        control: `<input type="url" id="pe-url" value="${escHtml(s.url)}" placeholder="http://example.com/guide.xml.gz">`,
      }) +
      fieldRow({
        label: 'Name', meta: 'Blank uses the provider name.',
        control: `<input type="text" id="pe-name" value="${escHtml(s.name)}">`, wide: true,
      }) +
      fieldRow({ label: 'Refresh', meta: 'How often the file is fetched again.',
        control: `<select id="pe-hours">${hours}</select>` }) +
      (editing ? fieldRow({
        label: 'On', meta: 'Turned off, the source keeps its listings and is not fetched again.',
        control: '<label class="switch"><input type="checkbox" id="pe-on"' +
          `${s.enabled ? ' checked' : ''}><span class="knob"></span></label>`,
      }) : '');
    const $m = (sel) => body.querySelector(sel);
    buildModal({
      title: editing ? 'Edit EPG source' : `Add EPG source to ${card.dataset.name}`,
      body,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        {
          label: editing ? 'Save' : 'Add source',
          class: 'btn btn-primary',
          onClick: (close) => {
            const url = $m('#pe-url').value.trim();
            if (!url) { showToast('An XMLTV URL is required.', { type: 'error' }); return false; }
            send(epgUrl(card, editing ? s.id : null), {
              method: 'POST',
              body: JSON.stringify({ url, name: $m('#pe-name').value,
                refresh_interval_hours: $m('#pe-hours').value,
                enabled: editing ? $m('#pe-on').checked : true }),
            }).then(() => close()).catch(() => {});
            return false;
          },
        },
      ],
    });
  }

  function editEpgSource(card, el) {
    jsonFetch(epgUrl(card, el.dataset.sourceId))
      .then((res) => openEpgSource(card, res.source))
      .catch((e) => showToast(e.message || 'Could not load the source.', { type: 'error' }));
  }

  function confirmEpgDelete(card, el) {
    const g = Number(el.dataset.guided || 0);
    confirmModal({
      title: 'Delete EPG source',
      message: `Delete ${el.dataset.sourceName} and every listing it imported, for every account on ${card.dataset.name}?`,
      consequence: g ? `${g.toLocaleString()} channel${g === 1 ? ' takes its' : 's take their'} guide from it now. ` +
        'Each switches to its next source, or has no guide.' : 'No channel takes its guide from it right now.',
      confirmLabel: 'Delete source',
      danger: true,
    }).then((ok) => {
      if (ok) send(epgUrl(card, el.dataset.sourceId), { method: 'DELETE' }).catch(() => {});
    });
  }

  function confirmEpgGive(card, el) {
    confirmModal({
      title: `Move to ${el.dataset.account} only`,
      message: `${el.dataset.sourceName} becomes ${el.dataset.account}'s alone and leaves ${card.dataset.name}'s order.`,
      consequence: 'Every other account reading it stops: its listings go from their channels, and each switches to its next source.',
      confirmLabel: 'Move',
    }).then((ok) => {
      if (ok) {
        send(`${epgUrl(card, el.dataset.sourceId)}/give`,
          { method: 'POST', body: JSON.stringify({ account_id: Number(el.dataset.accountId) }) })
          .catch(() => {});
      }
    });
  }

  function share(el) {
    send(`/api/logins/${el.dataset.loginId}/share`,
      { method: 'POST', body: JSON.stringify({ account_id: Number(el.dataset.accountId) }) })
      .catch(() => {});
  }

  function confirmUnshare(el) {
    confirmModal({
      title: 'Stop sharing login',
      message: `${el.dataset.account} stops holding login "${el.dataset.login}".`,
      consequence: 'The other account keeps it, with its seats.',
      confirmLabel: 'Stop sharing',
    }).then((ok) => {
      if (!ok) return;
      send(`/api/logins/${el.dataset.loginId}/share/${el.dataset.accountId}`,
        { method: 'DELETE' }).catch(() => {});
    });
  }

  function run(act, el) {
    const card = el.closest('[data-provider-id]');
    switch (act) {
      case 'prov-add': openAddProvider(); return;
      case 'prov-rename': openRename(card); return;
      case 'prov-add-account': openAddAccount(card); return;
      case 'prov-remove-account': confirmRemoveAccount(card, el); return;
      case 'prov-delete': confirmDelete(card); return;
      case 'prov-share': share(el); return;
      case 'prov-unshare': confirmUnshare(el); return;
      case 'prov-epg-add': openEpgSource(card, null); return;
      case 'prov-epg-edit': editEpgSource(card, el); return;
      case 'prov-epg-delete': confirmEpgDelete(card, el); return;
      case 'prov-epg-give': confirmEpgGive(card, el); return;
      case 'prov-epg-refresh':
        send(`${epgUrl(card, el.dataset.sourceId)}/refresh`, { method: 'POST' }).catch(() => {});
        return;
      case 'prov-epg-move':
        showToast('Moving it and re-deciding the guides of the accounts following this order...');
        send(`/api/providers/${card.dataset.providerId}/epg-order/${el.dataset.entryId}/move`,
          { method: 'POST', body: JSON.stringify({ direction: el.dataset.direction }) })
          .catch(() => {});
        return;
      default:
        console.warn('unhandled provider action', act);
    }
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('#acct-providers [data-act]');
    if (!el || el.disabled) return;
    e.preventDefault();
    closeMenus();
    run(el.dataset.act, el);
  });

  // ── The phone's bottom sheet (DESIGN.md §17.5) - util.js's openMenuSheet ─────
  function openSheet(btn) {
    const menu = btn.parentElement.querySelector('.menu');
    if (!menu) return;
    const card = btn.closest('[data-provider-id]');
    openMenuSheet(menu, {
      title: btn.getAttribute('aria-label') || (card && card.dataset.name) || '',
      // Run against the original item: it is still inside the card the action needs.
      onAct: (act, copy, original) => run(act, original || copy),
    });
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('#acct-providers [data-sheet]');
    if (!btn || !isPhone()) return;
    e.preventDefault();
    e.stopPropagation();
    openSheet(btn);
  }, true);
})();
