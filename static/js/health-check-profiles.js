/* Health Check Profiles list page (templates/health_check_profiles.html).

   Thin wiring only: the form itself lives in profile-modal.js, which both profile types
   share. This file owns the page's three triggers (Add, Edit, Delete) and nothing else.

   The delete confirm names the health checks that will be affected, because unlinking is
   silent otherwise - the checks keep running, they just fall back to the global defaults,
   and that is exactly the kind of invisible behavior change the app is supposed to say
   out loud (CLAUDE.md - nothing silent). */
(() => {
  const CFG = window.HCP_CONFIG || {};
  const byId = new Map((CFG.profiles || []).map((p) => [p.id, p]));

  const openEditor = (profile) => openHealthCheckProfileModal({
    profile,
    defaults: CFG.defaults,
  });

  function openDelete(profile) {
    const used = (CFG.jobCounts || {})[profile.id] || 0;
    const fallout = used
      ? `<p style="font-size:.9rem" class="text-muted">${used} health check${used === 1 ? '' : 's'} ` +
        `use${used === 1 ? 's' : ''} it right now. ${used === 1 ? 'It' : 'They'} will keep running, ` +
        'but fall back to your global defaults.</p>'
      : '';
    buildModal({
      title: 'Delete profile',
      body: `<p style="font-size:.9rem">"${escHtml(profile.name)}" will be deleted.</p>${fallout}`,
      footer: [
        { label: 'Cancel', class: 'btn', onClick: (c) => c() },
        { label: 'Delete profile', class: 'btn btn-danger', onClick: (close) => {
          jsonFetch(`/api/health-check-profiles/${profile.id}`, { method: 'DELETE' })
            .then(() => window.location.reload())
            .catch((err) => { close(); showToast(err.message, { type: 'error' }); });
          return false;
        } },
      ],
    });
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#hcp-add, #hcp-add-empty')) { openEditor(null); return; }
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const profile = byId.get(Number(act.dataset.profile));
    if (!profile) return;
    if (act.dataset.act === 'edit') openEditor(profile);
    else if (act.dataset.act === 'delete') openDelete(profile);
  });
})();
