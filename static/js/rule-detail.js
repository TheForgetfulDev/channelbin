/* One auto-record rule's page (templates/rule_detail.html, mockup 42, dev/changelog/1189).

   Thin wiring over the actions every Rules surface shares (auto-rule-modal.js). After an
   action the header, the banners and status bar, and the cards are swapped for a fresh
   server render (swapFromServer), so the template is the one renderer and the badge, the
   counts and every card follow it. Two things are drawn here, and both are redrawn after
   every swap:

   * The Settings chips: util.js renderSettingChips() over #rule-setbar's data-items, which
     arrives with the swap. Their arrangement follows the 960px breakpoint, so crossing it
     redraws them too.
   * Nothing else - History's filter chips and "Show more" are links to this page with
     ?hist= and ?shown=, so they work with a plain or a new-tab click; a plain click swaps
     only the History card and puts the URL in the address bar, so a reload keeps it.

   The section layout is util.js initSectionLayout(): a generated stylesheet orders and
   hides the [data-section] cards, so a swap never undoes it. */
(() => {
  const CFG = window.RULE_CONFIG || {};
  const PHONE = MOBILE_MQ;  // util.js's one breakpoint
  // Read from the status bar each time: a swap replaces it with the rule as it is now.
  const bar = () => document.getElementById('rule-actionbar');
  const dialog = () => JSON.parse(bar().dataset.dialog);
  const rule = () => {
    const d = dialog();
    return { id: d.id, name: d.name, scheduled: Number(bar().dataset.scheduled) || 0 };
  };

  function drawSettings() {
    const host = document.getElementById('rule-setbar');
    if (!host) return;
    renderSettingChips(host, JSON.parse(host.dataset.items || '[]'),
                       { phone: PHONE.matches, act: 'edit' });
  }

  /* After an action. A failed re-render still leaves the action done, so it falls back to
     a full load rather than leaving the page showing the rule as it was. */
  async function refresh() {
    try {
      await swapFromServer(['.gd-head', '.gd-statezone', '#rule-sections']);
    } catch (e) {
      window.location.reload();
      return;
    }
    drawSettings();
  }

  async function history(url) {
    try {
      await swapFromServer(['#rule-history'], url);
    } catch (e) {
      followHref(null, url);
      return;
    }
    window.history.replaceState(null, '', url);
  }

  const sectionLayout = initSectionLayout({
    config: { sections: CFG.sections, sectionPref: CFG.sectionPref },
    saveUrl: `/api/user-prefs/${CFG.sectionsPrefKey}`,
    names: CFG.sectionNames || {},
    note: 'The title, status bar and warning banners always show. The same layout applies to every rule.',
  });

  document.addEventListener('click', (e) => {
    const link = e.target.closest('[data-hist], [data-hist-more]');
    if (link) {
      if (wantsNewTab(e)) return;
      e.preventDefault();
      history(link.href);
      return;
    }
    const act = e.target.closest('[data-act]');
    if (!act || act.disabled) return;
    closeMenus();
    const name = act.dataset.act;
    if (name === 'pause') autoRulePause(rule(), CFG, refresh);
    else if (name === 'resume') autoRuleResume(rule(), CFG, refresh);
    else if (name === 'check') autoRuleCheck(rule(), CFG, refresh);
    else if (name === 'sections') sectionLayout.open();
    else if (name === 'delete') {
      openAutoRuleDelete(rule(), CFG, () => {
        window.location.href = CFG.rulesUrl;  // nav-ok: the rule this page showed is gone
      });
    } else if (name === 'edit') {
      openAutoRuleModal({
        rule: dialog(), focus: act.dataset.focus, apiBase: CFG.apiBase,
        profiles: CFG.profiles, defaultCap: CFG.defaultCap, phrases: CFG.phrases,
        onSaved: refresh,
      });
    }
  });

  if (PHONE.addEventListener) PHONE.addEventListener('change', drawSettings);
  drawSettings();
})();
