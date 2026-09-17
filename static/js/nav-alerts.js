/* The nav's alert surfaces: the Alerts link's unread counts, the collapsed rail's pip, and
   the banner shown above every page but Alerts, with its details view. Loaded by base.html
   on every page. Rules: dev/changelog/923; built in dev/changelog/924.

   window.__applyAlerts is the ONE updater for all three regions. /api/nav-status feeds it
   (app/routes/alerts.py::_unread_alert_summary), and the Alerts page calls it with just
   the counts so they move on a click instead of at the next poll - never writing these
   nodes itself.

   * Red counts unread ERROR + CRIT, yellow counts WARN, INFO is never counted, and each
     hides at zero. Both nav copies (sidebar and phone drawer) carry the pair.
   * The rail has room for one mark, so the pip takes the worst color present.
   * The banner shows the alert the server picked (the most severe unread one). Its title
     opens details, × marks it read in one click, "+N more" goes to Alerts.
   * Details has two buttons, Mark read and the destination. "Ignore future alerts like
     this" stays reachable from here (dev/changelog/615), one level down behind ⋯. */
(() => {
  'use strict';

  let shown = null;

  const setShown = (el, on) => { el.style.display = on ? '' : 'none'; };
  const refresh = () => { if (window.fetchNavStatus) window.fetchNavStatus(); };

  function applyCounts({ error_count: red = 0, warn_count: warn = 0 }) {
    document.querySelectorAll('.nav-count-bad').forEach((el) => {
      el.textContent = red;
      setShown(el, red > 0);
    });
    document.querySelectorAll('.nav-count-warn').forEach((el) => {
      el.textContent = warn;
      setShown(el, warn > 0);
    });
    // The pip stands in for both counts on the rail, so it hides with them - a mark that
    // never clears would claim an alert forever.
    document.querySelectorAll('.nav-pip.pip-alert').forEach((pip) => {
      pip.classList.toggle('pip-bad', red > 0);
      setShown(pip, red + warn > 0);
    });
  }

  function applyBanner(a, more) {
    shown = a || null;
    const banner = document.getElementById('alert-banner');
    if (!banner) return;
    if (!a) { setShown(banner, false); return; }
    banner.className = `alert-banner alert-banner-sev-${a.severity}`;
    const sev = document.getElementById('alert-banner-sev');
    sev.className = `alert-severity sev-${a.severity}`;
    sev.textContent = a.severity;
    document.getElementById('alert-banner-title').textContent = a.title;
    // When it happened, outside the truncating span so the ellipsis cannot swallow it. An
    // alert that recovered weeks ago reads identically to one raised a minute ago without
    // this, which is how a stale row went on looking urgent (dev/changelog/939).
    const time = document.getElementById('alert-banner-time');
    if (time) {
      time.textContent = a.created_short || '';
      if (a.created_age) time.setAttribute('data-tip', `Raised ${a.created_age}.`);
      else time.removeAttribute('data-tip');
    }
    const moreLink = document.getElementById('alert-banner-more');
    moreLink.textContent = `+${more} more`;
    moreLink.setAttribute('data-tip',
      `${more} more unread.\nOpens Alerts. The banner always shows the most severe one first.`);
    setShown(moreLink, more > 0);
    setShown(banner, true);
  }

  // Off the banner at once; the refresh that follows the POST brings the next one up, or
  // puts this one back if the POST failed.
  function markRead(a) {
    if (shown && shown.id === a.id) applyBanner(null, 0);
    jsonFetch(`/api/alerts/${a.id}/read`, { method: 'POST' })
      .catch((e) => showToast(`Could not mark that alert read: ${e.message}`, { type: 'error' }))
      .finally(refresh);
  }

  // Shown for an alert that is still true and clears itself (dev/changelog/932). Without
  // it, Mark read looks like it disposed of the problem: the banner goes and the count
  // drops, while the condition it named is still live and still listed under Active alerts.
  function activeNotice(severity) {
    const el = document.createElement('div');
    const red = severity === 'CRIT' || severity === 'ERROR';
    el.className = `notice notice-${red ? 'bad' : 'warn'}`;
    const label = document.createElement('strong');
    label.textContent = 'Still active.';
    el.append(label, document.createTextNode(
      ' It clears itself once this is fixed. Marking it read takes it off the banner and out '
      + 'of the count, and it stays under Active alerts until then.'));
    return el;
  }

  function openDetails(a) {
    const body = document.createElement('div');
    const meta = document.createElement('div');
    meta.className = 'alert-detail-meta';
    const sev = document.createElement('span');
    sev.className = `alert-severity sev-${a.severity}`;
    sev.textContent = a.severity;
    const when = document.createElement('span');
    when.textContent = a.created_label || '';
    meta.append(sev, when);
    // The absolute time with its age beside it, which is DESIGN.md 5's app-wide pairing.
    // The banner itself has no second line for the age, so this is where it lands.
    if (a.created_age) {
      const age = document.createElement('span');
      age.textContent = `· ${a.created_age}`;
      meta.append(age);
    }
    const text = document.createElement('div');
    text.className = 'alert-detail-body';
    text.textContent = a.body || a.title;
    body.append(meta);
    if (a.is_active_problem) body.append(activeNotice(a.severity));
    body.append(text);

    // The destination is the primary action when there is one: an alert that names a
    // recording or an account is mostly opened in order to go and look at it.
    const footer = [{
      label: 'Mark read', class: a.link ? 'btn' : 'btn btn-primary',
      onClick: (close) => { close(); markRead(a); },
    }];
    if (a.link) {
      footer.push({
        label: `${a.link_label} →`, class: 'btn btn-primary',
        onClick: (close) => { close(); window.location.href = a.link; },  // nav-ok: modal button
      });
    }
    const overlay = buildModal({ title: a.title, body, footer });

    const more = document.createElement('span');
    more.className = 'menu-wrap alert-detail-more';
    more.innerHTML = '<button type="button" class="btn btn-icon" data-menu aria-label="More actions">&#8943;</button>'
      + '<div class="menu pop-up pop-left">'
      + '<button type="button" class="menu-item">Ignore future alerts like this</button></div>';
    more.querySelector('.menu-item').addEventListener('click', () => {
      closeMenus();
      overlay.closeModal();
      confirmIgnoreAlert(a.id, a.title, () => {
        if (shown && shown.id === a.id) applyBanner(null, 0);
        refresh();
      });
    });
    overlay.querySelector('.modal-foot').prepend(more);
  }

  document.addEventListener('click', (e) => {
    if (!shown) return;
    if (e.target.closest('#alert-banner-title')) {
      e.preventDefault();
      openDetails(shown);
    } else if (e.target.closest('#alert-banner-read')) {
      markRead(shown);
    }
  });
  // The title is an <a role="button">, so Enter already clicks it; Space is the other half
  // of what a button promises.
  document.addEventListener('keydown', (e) => {
    if (e.key !== ' ' || !shown || !e.target.closest || !e.target.closest('#alert-banner-title')) return;
    e.preventDefault();
    openDetails(shown);
  });

  window.__applyAlerts = (d) => {
    applyCounts(d);
    if ('banner' in d) applyBanner(d.banner, d.more || 0);
  };
})();
