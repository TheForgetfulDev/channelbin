/* The Rules list (templates/rules.html, mockup 42, dev/changelog/1189).

   Thin wiring: a row opens its rule's page, and each row's kebab runs the actions every
   Rules surface shares (auto-rule-modal.js) - Pause, Resume, Edit settings, Check now,
   Delete rule - followed by a fresh server render of the page head and the list
   (swapFromServer), so the template stays the one renderer and the counts, the row and the
   empty state follow every action. Each row carries what the actions need as data
   attributes, which come back with that render. Edit search and Open search are links. */
(() => {
  const CFG = window.RULES_CONFIG || {};

  const ruleOf = (el) => {
    const row = el.closest('.row[data-rule]');
    if (!row) return null;
    return {
      id: Number(row.dataset.rule),
      name: row.dataset.name || '',
      scheduled: Number(row.dataset.scheduled) || 0,
      dialog: JSON.parse(row.dataset.dialog || 'null'),
    };
  };

  // A failed re-render still leaves the action done, so fall back to a full load rather
  // than leaving the page showing the rule as it was.
  const refresh = () => swapFromServer(['.page-head', '#rules-body'])
    .catch(() => window.location.reload());

  // A real link inside the row (the name) navigates on its own; the kebab and the
  // tooltip targets are the row's dead zones, as on the accounts list.
  const NO_NAV = '.c-actions, .menu, a, [data-tip]';
  bindNavClicks(document, (e) => {
    const row = e.target.closest('.rl-list .row[data-href]');
    return row && !e.target.closest(NO_NAV) ? row.dataset.href : null;
  });

  document.addEventListener('click', (e) => {
    const act = e.target.closest('[data-act]');
    if (!act || act.disabled) return;
    const rule = ruleOf(act);
    if (!rule) return;
    closeMenus();
    const name = act.dataset.act;
    if (name === 'pause') autoRulePause(rule, CFG, refresh);
    else if (name === 'resume') autoRuleResume(rule, CFG, refresh);
    else if (name === 'check') autoRuleCheck(rule, CFG, refresh);
    else if (name === 'delete') openAutoRuleDelete(rule, CFG, refresh);
    else if (name === 'edit') {
      openAutoRuleModal({
        rule: rule.dialog, apiBase: CFG.apiBase, profiles: CFG.profiles,
        defaultCap: CFG.defaultCap, phrases: CFG.phrases, onSaved: refresh,
      });
    }
  });
})();
