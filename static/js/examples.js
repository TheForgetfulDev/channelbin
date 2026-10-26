/* Example channels (app/examples.py, dev/changelog/1281, 1288): "Try it with example channels",
   Delete the examples and Keep the examples, on the Get started page and the example
   account's own page. The Accounts list and the account page's sheet reach the delete through
   window.removeExamples, since their menus dispatch by data-act.

   Delegated from the document, because the Get started page swaps #gs-live (and the buttons
   in it) for a fresh server copy whenever the nav poll moves. The confirm dialog for a delete
   is filled from GET /api/examples/removal, the same plan the delete follows, so it names
   exactly what goes and what stays - or, when something stands in the way, says that instead
   of offering a button that would only be refused. */
(() => {
  const busy = new WeakSet();
  // A menu item reached through window.removeExamples may come without a button to hold.
  const hold = (btn) => { if (btn) { busy.add(btn); btn.disabled = true; } };
  const release = (btn) => { if (btn) { btn.disabled = false; busy.delete(btn); } };

  async function load(btn) {
    const ok = await confirmModal({
      title: 'Load the example channels?',
      message: 'ChannelBin adds the M3UPT example account and syncs it now, then puts a few '
        + 'channels in the TV Guide, makes the example channel group and runs one health check.',
      confirmLabel: 'Load the examples',
    });
    if (!ok) return;
    hold(btn);
    // No toast on success: the reloaded page shows the load's progress itself, and a toast
    // sat on top of exactly that text (dev/changelog/1288).
    try {
      await jsonFetch('/api/examples', { method: 'POST' });
      window.location.reload();
    } catch (err) {
      showToast(err.message, { type: 'error', durationMs: 9000 });
      release(btn);
    }
  }

  function refusal(blockers) {
    buildModal({
      title: 'The examples cannot be deleted yet',
      body: `<p>${escHtml(blockers.length === 1 ? 'One thing stands in the way:' : 'These stand in the way:')}</p>`
        + `<ul class="confirm-list">${blockers.map((b) => `<li>${escHtml(b)}</li>`).join('')}</ul>`,
      footer: [{ label: 'Close' }],
    });
  }

  async function remove(btn) {
    let plan;
    try {
      plan = await jsonFetch('/api/examples/removal');
    } catch (err) {
      showToast(err.message, { type: 'error', durationMs: 9000 });
      return;
    }
    if (plan.blockers.length) { refusal(plan.blockers); return; }
    const ok = await confirmModal({
      title: 'Delete the examples?',
      message: 'This deletes:',
      list: plan.lines,
      consequence: 'It cannot be undone. While there is no account, Get started offers the '
        + 'examples again.',
      confirmLabel: 'Delete the examples',
      danger: true,
    });
    if (!ok) return;
    hold(btn);
    try {
      const res = await jsonFetch('/api/examples/remove', { method: 'POST' });
      showToastAfterLoad(res.message, { durationMs: 9000 });
      window.location.href = res.next_url;  // nav-ok: the example account's page this may run from is gone
    } catch (err) {
      showToast(err.message, { type: 'error', durationMs: 12000 });
      release(btn);
    }
  }

  async function keep(btn) {
    const ok = await confirmModal({
      title: 'Keep the examples?',
      message: 'The example account and the example channel group become ordinary ones, '
        + 'for when M3UPT is all you want. Delete the examples no longer applies to them, and '
        + 'Get started counts the account as yours.',
      confirmLabel: 'Keep the examples',
    });
    if (!ok) return;
    hold(btn);
    try {
      const res = await jsonFetch('/api/examples/keep', { method: 'POST' });
      showToastAfterLoad(res.message, { durationMs: 9000 });
      window.location.reload();
    } catch (err) {
      showToast(err.message, { type: 'error', durationMs: 9000 });
      release(btn);
    }
  }

  window.removeExamples = remove;

  document.addEventListener('click', (e) => {
    const loadBtn = e.target.closest('[data-examples-load]');
    if (loadBtn && !busy.has(loadBtn)) { load(loadBtn); return; }
    const removeBtn = e.target.closest('[data-examples-remove]');
    if (removeBtn && !busy.has(removeBtn)) { remove(removeBtn); return; }
    const keepBtn = e.target.closest('[data-examples-keep]');
    if (keepBtn && !busy.has(keepBtn)) keep(keepBtn);
  });
})();
