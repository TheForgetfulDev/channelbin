/* Recording Profiles list page (templates/profiles.html).

   Thin wiring only: the form itself lives in profile-modal.js, which both profile types
   share. This file owns the page's three triggers (Add, Edit, Delete) and nothing else.

   The delete confirm names what is attached, because unlinking is silent otherwise - the
   recordings and channels keep working, they just fall back to the global settings, and
   that is exactly the kind of invisible behavior change the app is supposed to say out
   loud (CLAUDE.md - nothing silent). */
(() => {
  const CFG = window.RP_CONFIG || {};
  const byId = new Map((CFG.profiles || []).map((p) => [p.id, p]));

  const openEditor = (profile) => openRecordingProfileModal({
    profile,
    defaults: CFG.defaults,
  });

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  function openDelete(profile) {
    const recs = (CFG.recCounts || {})[profile.id] || 0;
    const chans = (CFG.channelCounts || {})[profile.id] || 0;
    const attached = [];
    if (recs) attached.push(plural(recs, 'recording'));
    if (chans) attached.push(`${plural(chans, 'channel')} using it as a default`);
    const fallout = attached.length
      ? `<p style="font-size:.9rem" class="text-muted">${escHtml(attached.join(' and '))} ` +
        'will fall back to your global settings. Nothing is deleted except the profile ' +
        'itself.</p>'
      : '';
    buildModal({
      title: 'Delete profile',
      body: `<p style="font-size:.9rem">"${escHtml(profile.name)}" will be deleted.</p>${fallout}`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        { label: 'Delete profile', class: 'btn btn-danger', onClick: (close) => {
          jsonFetch(`/api/profiles/${profile.id}`, { method: 'DELETE' })
            .then(() => window.location.reload())
            .catch((err) => { close(); showToast(err.message, { type: 'error' }); });
          return false;
        } },
      ],
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#rp-add, #rp-add-empty')) { openEditor(null); return; }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const profile = byId.get(Number(act.dataset.profile));
    if (!profile) return;
    if (act.dataset.act === 'edit') openEditor(profile);
    else if (act.dataset.act === 'delete') openDelete(profile);
  });
})();
