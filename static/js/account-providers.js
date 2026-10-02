/* The Accounts page's Providers section (DESIGN-account-providers.md §6.2,
   dev/changelog/1170): create, rename and delete a provider, put an account on one or take
   it off, share a login with another account on the same provider or stop sharing it.

   Every action is one request to /api/providers* or /api/logins/<id>/share, then a
   server-rendered swap of #acct-providers - the section has exactly one renderer, the
   template. Handlers are delegated from the document, so a swapped section needs no
   rebinding. On a phone a kebab or a login chip opens a bottom sheet holding the same
   menu items, as the account rows' kebab does.

   Depends on util.js (escHtml, jsonFetch, showToast, buildModal, confirmModal, fieldRow,
   closeMenus, swapFromServer). */
(() => {
  const isPhone = () => window.matchMedia('(max-width: 960px)').matches;
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
      return `<label class="prov-check"><input type="checkbox" value="${c.id}"> ` +
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

  function confirmDelete(card) {
    confirmModal({
      title: 'Delete provider',
      message: `Accounts on ${card.dataset.name} will be unlinked.`,
      consequence: 'Their channels will stop folding together. Their hosts and logins ' +
        'stay as they are, and a login two of them share stays shared until you change it.',
      confirmLabel: 'Delete provider',
      danger: true,
    }).then((ok) => {
      if (!ok) return;
      send(`/api/providers/${card.dataset.providerId}`, { method: 'DELETE' }).catch(() => {});
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

  // ── The phone's bottom sheet (DESIGN.md §17.5) ──────────────────────────
  // The sheet's buttons are copies of the menu's own items, data attributes included, so
  // the two surfaces cannot offer different actions.
  function openSheet(btn) {
    const menu = btn.parentElement.querySelector('.menu');
    if (!menu) return;
    const card = btn.closest('[data-provider-id]');
    const body = document.createElement('div');
    body.className = 'acct-sheet';
    Array.from(menu.children).forEach((item) => {
      if (item.classList.contains('sep')) {
        body.insertAdjacentHTML('beforeend', '<div class="sheet-sep"></div>');
        return;
      }
      const copy = item.cloneNode(true);
      copy.className = `sheet-act${item.classList.contains('danger') ? ' danger' : ''}`;
      body.appendChild(copy);
    });
    const sheet = buildModal({ title: btn.getAttribute('aria-label') || (card && card.dataset.name) || '', body });
    body.addEventListener('click', (e) => {
      const item = e.target.closest('button[data-act]');
      if (!item || item.disabled) return;
      e.stopPropagation();
      sheet.closeModal();
      // Run against the original item: it is still inside the card the action needs.
      const original = Array.from(menu.querySelectorAll('[data-act]'))
        .find((m) => m.dataset.act === item.dataset.act
          && m.dataset.accountId === item.dataset.accountId
          && m.dataset.loginId === item.dataset.loginId);
      run(item.dataset.act, original || item);
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
