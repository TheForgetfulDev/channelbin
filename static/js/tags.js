/* Tags list page (templates/tags.html).

   Thin wiring only: the form itself lives in tag-modal.js. This file owns the page's four
   triggers (Add, Add-from-empty-state, Edit, Delete) and the ?new=1 entry point.

   The delete confirm names every place the tag's NAME is spelled out, because a tag is
   referenced by name rather than by id: a {tag:live} placeholder in a filename template
   does not error when the tag disappears, it just quietly renders nothing from then on.
   Saying so beforehand is the whole point (CLAUDE.md - nothing silent). */
(() => {
  const CFG = window.TAG_CONFIG || {};
  const byId = new Map((CFG.tags || []).map((t) => [t.id, t]));

  const openEditor = (tag) => openTagModal({
    tag,
    presetColors: CFG.presetColors,
  });

  function openDelete(tag) {
    const used = (CFG.usage || {})[tag.name] || [];
    const fallout = used.length
      ? `<p style="font-size:.9rem" class="text-muted">Its name is still referenced by ` +
        `${escHtml(used.join(', '))}. Nothing there is deleted or rewritten - those ` +
        'references simply stop resolving, so check them afterwards.</p>'
      : '';
    buildModal({
      title: 'Delete tag',
      body: `<p style="font-size:.9rem">"${escHtml(tag.name)}" and its ` +
        `${tag.patterns.length} match pattern${tag.patterns.length === 1 ? '' : 's'} ` +
        `will be deleted.</p>${fallout}`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        { label: 'Delete tag', class: 'btn btn-danger', onClick: (close) => {
          jsonFetch(`/api/tags/${tag.id}`, { method: 'DELETE' })
            .then(() => window.location.reload())
            .catch((err) => { close(); showToast(err.message, { type: 'error' }); });
          return false;
        } },
      ],
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#tag-add, #tag-add-empty')) { openEditor(null); return; }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const tag = byId.get(Number(act.dataset.tag));
    if (!tag) return;
    if (act.dataset.act === 'edit') openEditor(tag);
    else if (act.dataset.act === 'delete') openDelete(tag);
  });

  // ?new=1 opens the create modal straight away - the deep link the channel search's
  // "+ Create tag" affordance uses now that /tags/new is gone (static/js/channel-search.js).
  // The parameter is dropped from the URL so a reload after saving does not reopen it.
  if (new URLSearchParams(window.location.search).get('new') === '1') {
    window.history.replaceState({}, '', window.location.pathname);
    openEditor(null);
  }
})();
