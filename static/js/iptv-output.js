/* The IPTV Output page (templates/iptv_output.html, mockup 53, dev/changelog/1526).

   Every action posts, then the page's regions are swapped for the server's fresh render
   (swapFromServer), so the template stays the one renderer. What the swap would lose is held
   here and put back after it: the connection tab and the channel list's search, category,
   chip and page. Clicks are delegated, since a swap rebuilds the nodes. The live regions are swapped
   again on a timer so Watching now and Recent plays keep up while the page is open.

   The password is not in the page. The eye and Copy ask /api/settings/reveal for it, the same
   gated read the Settings page's eye uses.

   Three sizes of swap. Turning the output on or off, the login and the mode change which cards
   exist, so they take everything. A switch or a stopped play takes the cards but not Settings,
   and a saved number takes only the state bar, so a number being typed in another settings
   row is never replaced by the save of the one before it. */
(() => {
  const ALL = ['#of-state', '#of-sections'];
  const CARDS = ['#of-state', '#of-connect', '#of-watching', '#of-channels', '#of-recent', '#of-accounts'];
  const STATE_ONLY = ['#of-state'];
  // What moves on its own. The channel list and the settings rows are left out of the timer,
  // so a search in progress or a number being typed is never replaced under the person.
  const LIVE = ['#of-state', '#of-watching', '#of-recent', '#of-accounts'];
  const LIVE_MS = 15 * 1000;
  const PASSWORD_PATH = 'integrations.output_feed.password';
  const FIELD_URL = '/api/settings/field';

  const view = { tab: 'xtream', q: '', cat: '', show: 'all', page: 1 };
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const players = (n) => `${n} IPTV player${n === 1 ? '' : 's'}`;
  const post = (url, body) => jsonFetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
  const fail = (err) => showToast(err.message || 'That did not work.', { type: 'error' });

  /* ---- Held state, re-applied after every swap ------------------------------------------- */
  function applyTab() {
    $$('#of-connect [data-tab]').forEach((b) => {
      const on = b.dataset.tab === view.tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('#of-connect [data-pane]').forEach((p) => { p.hidden = p.dataset.pane !== view.tab; });
  }

  function rowMatches(tr) {
    const state = tr.dataset.state;
    if (view.show === 'on' && state !== 'on') return false;
    if (view.show === 'hidden' && state !== 'hidden') return false;
    if (view.show === 'problem' && tr.dataset.problem !== '1') return false;
    if (view.cat && tr.dataset.cat !== view.cat) return false;
    const q = view.q.trim().toLowerCase();
    return !q || tr.dataset.hay.includes(q);
  }

  function applyFilter() {
    const rows = $$('#of-rows tr[data-feed]');
    if (!rows.length) return;
    const cat = $('#of-cat');
    // A category whose last channel left the guide goes with it.
    if (view.cat && !Array.from(cat.options).some((o) => o.value === view.cat)) view.cat = '';
    cat.value = view.cat;
    const q = $('#of-q');
    if (q.value !== view.q) q.value = view.q;
    // The channels are paged over what the search and the filters leave, so a match on a
    // later page is never hidden behind a page of rows that did not match.
    const per = Number($('#of-rows').dataset.perPage) || rows.length;
    const matched = rows.filter(rowMatches);
    const pages = Math.max(1, Math.ceil(matched.length / per));
    view.page = Math.min(Math.max(1, view.page), pages);
    const first = (view.page - 1) * per;
    const onPage = new Set(matched.slice(first, first + per));
    rows.forEach((tr) => { tr.hidden = !onPage.has(tr); });
    $('#of-none').hidden = matched.length > 0;
    const pager = $('#of-chan-pager');
    pager.hidden = pages === 1;
    $('#of-chan-n').textContent = `${first + 1}-${first + onPage.size} of ${matched.length}`;
    $('[data-dir="-1"]', pager).disabled = view.page === 1;
    $('[data-dir="1"]', pager).disabled = view.page === pages;
    $$('#of-chips [data-show]').forEach((chip) => {
      const on = chip.dataset.show === view.show;
      chip.classList.toggle('active', on);
      chip.classList.toggle('tone-warn', on && chip.dataset.show === 'problem');
    });
  }

  const applyView = () => { applyTab(); applyFilter(); };

  /* ---- Swapping ---------------------------------------------------------------------------
     One after another: a timed swap fetched before an action's must never land after it and
     put back what the action just changed. */
  let swapQueue = Promise.resolve();
  let swapsPending = 0;
  const swap = (regions) => {
    swapsPending += 1;
    const run = swapQueue.then(() => swapFromServer(regions)).then(applyView);
    swapQueue = run.catch(() => {}).finally(() => { swapsPending -= 1; });
    return run;
  };
  const refresh = (regions = ALL) => swap(regions).catch(() => window.location.reload());

  const inUse = () => LIVE.some((sel) => {
    const region = $(sel);
    if (!region) return false;
    const field = document.activeElement;
    return (field && region.contains(field) && field.matches('select')) || hasSelectionIn(region);
  });
  const liveTick = () => {
    if (document.hidden || swapsPending || overlayOpen() || inUse()) return;
    const before = $('#of-state').dataset.state;
    swap(LIVE)
      // Turned on or off from somewhere else: the cards themselves differ, so take them all.
      .then(() => ($('#of-state').dataset.state === before ? null : swap(ALL)))
      .catch((err) => console.warn('IPTV Output refresh failed; the next turn retries.', err));
  };

  /* ---- The password, on request ---------------------------------------------------------- */
  const fetchPassword = () => jsonFetch(`/api/settings/reveal?path=${encodeURIComponent(PASSWORD_PATH)}`)
    .then((d) => d.value || '');
  const fill = (template, password) => template.split('{password}').join(encodeURIComponent(password));

  async function copyRow(btn) {
    const row = btn.closest('.of-conn');
    let text;
    if (btn.dataset.secret === undefined) {
      text = $('[data-copy-value]', row).textContent;
    } else {
      const password = await fetchPassword();
      // The Xtream password is typed as it is; inside an address it is percent-encoded.
      text = btn.dataset.secret === '{password}' ? password : fill(btn.dataset.secret, password);
    }
    await copyText(text);
    showToast(`${btn.dataset.label} copied.`);
  }

  async function revealAddress(btn) {
    const value = $('[data-secret-text]', btn.closest('.of-conn'));
    const shown = btn.dataset.shown === '1';
    const template = value.dataset.secretText;
    // What the server drew, kept so hiding puts back exactly that.
    if (value.dataset.masked === undefined) value.dataset.masked = value.textContent;
    if (shown) value.textContent = value.dataset.masked;
    // The Xtream password is shown as it is typed; inside an address it is percent-encoded.
    else if (template === '{password}') value.textContent = await fetchPassword();
    else value.textContent = fill(template, await fetchPassword());
    btn.dataset.shown = shown ? '' : '1';
    btn.innerHTML = icon(shown ? 'reveal' : 'conceal');
    btn.setAttribute('aria-label', `${shown ? 'Show' : 'Hide'} the password`);
  }

  /* ---- Actions ---------------------------------------------------------------------------- */
  async function setPower(to) {
    if (to === 'off') {
      const n = Number($('#of-state').dataset.watching) || 0;
      const ok = await confirmModal({
        title: 'Turn off the IPTV output?',
        message: 'No IPTV player can connect until it is turned on again.',
        consequence: n ? `${players(n)} watching right now ${n === 1 ? 'is' : 'are'} cut.` : '',
        confirmLabel: 'Turn off',
      });
      if (!ok) return;
    }
    await post(FIELD_URL, { path: 'integrations.output_feed.enabled', value: to === 'on' });
    await refresh();
    if (to === 'on') showToast('The IPTV output is on.');
  }

  async function setRow(input) {
    const tr = input.closest('tr');
    const name = tr.dataset.name;
    const on = input.checked;
    const watchers = Number(tr.dataset.watching) || 0;
    if (!on && watchers) {
      const upNext = tr.dataset.upNext === '1';
      const ok = await confirmModal({
        title: upNext ? `Turn off ${name}?` : `Hide ${name}?`,
        message: `${players(watchers)} ${watchers === 1 ? 'is' : 'are'} watching it right now, and ${upNext ? 'turning it off' : 'hiding it'} stops that play.`,
        confirmLabel: upNext ? 'Turn off' : 'Hide',
      });
      if (!ok) { input.checked = true; return; }
    }
    try {
      const d = await post(`/api/iptv-output/rows/${tr.dataset.feed}`, { on });
      await refresh(CARDS);
      const cut = d.cut ? ` ${players(d.cut)} watching it ${d.cut === 1 ? 'was' : 'were'} cut.` : '';
      // An Up next channel is ChannelBin's own, in no TV Guide, and off until a person turns it on.
      if (tr.dataset.upNext === '1') {
        showToast(on ? `${name} is on the IPTV output. An IPTV player shows it the next time it refreshes its channel list.`
          : `${name} is off the IPTV output. An IPTV player drops it the next time it refreshes its channel list.${cut}`);
      } else if (on) showToast(`${name} is back on the IPTV output, as the same channel it was before.`);
      else {
        showToast(`${name} is hidden from the IPTV output and stays in your TV Guide. An IPTV player drops it the next time it refreshes its channel list.${cut}`);
      }
    } catch (err) {
      input.checked = !on;
      fail(err);
    }
  }

  async function setAccount(input) {
    const name = input.dataset.name;
    const allow = input.checked;
    try {
      const d = await post(`/api/iptv-output/accounts/${input.dataset.id}`, { allow });
      await refresh(CARDS);
      if (allow) showToast(`The IPTV output may play from ${name} again.`);
      else {
        showToast(`The IPTV output no longer plays from ${name}.${d.cut ? ` ${players(d.cut)} playing from it ${d.cut === 1 ? 'was' : 'were'} cut.` : ''}`,
          { type: d.cut ? 'warning' : 'success' });
      }
    } catch (err) {
      input.checked = !allow;
      fail(err);
    }
  }

  async function setHeld(select) {
    const n = Number(select.value);
    const name = select.dataset.name;
    try {
      await post(`/api/iptv-output/accounts/${select.dataset.id}`, { held: n });
      await refresh(CARDS);
      showToast(n ? `Saved. Recordings now leave ${n} connection${n === 1 ? '' : 's'} of ${name} alone.`
        : `Saved. Recordings may use every connection of ${name}.`);
    } catch (err) {
      fail(err);
      refresh(CARDS);
    }
  }

  async function stopPlay(btn) {
    const ok = await confirmModal({
      title: 'Stop this play?',
      message: `The IPTV player at ${btn.dataset.client} stops playing ${btn.dataset.name} and its connection is free again.`,
      consequence: 'Nothing stops the player from tuning in again. To keep it off a channel, hide the channel.',
      confirmLabel: 'Stop',
      danger: true,
    });
    if (!ok) return;
    try {
      await post(`/api/iptv-output/plays/${encodeURIComponent(btn.dataset.id)}/stop`);
    } catch (err) {
      fail(err);
    }
    refresh(CARDS);
  }

  function openLogin(btn) {
    const first = btn.dataset.first === '1';
    const body = `<div class="of-login"><p>${first
      ? 'An IPTV player sends these every time it asks for anything. Short is fine: they are typed with a remote. A short login is one more reason to keep ChannelBin on your home network: do not open it to the internet.'
      : 'Every IPTV player using the old login is refused the next time it asks for anything, until the new one is typed into it. A play that is already running carries on until then.'}</p>
      <label for="of-user">Username</label>
      <input class="form-control" id="of-user" value="${escHtml(btn.dataset.user || '')}" autocomplete="off" maxlength="60">
      <label for="of-pass">Password</label>
      <input class="form-control" id="of-pass" autocomplete="off" maxlength="60"${first ? '' : ' placeholder="Leave empty to keep the password you have"'}>
      <p class="card-note">Letters and numbers only keeps it easy to type on a TV. The password is kept as typed, so this page can show it back to you.</p></div>`;
    const overlay = buildModal({
      title: first ? 'Set the IPTV player\'s login' : 'Change the IPTV player\'s login',
      body,
      footer: [
        { label: 'Cancel', class: 'btn' },
        { label: 'Save', class: 'btn btn-primary', onClick: (close) => {
          const username = $('#of-user', overlay).value.trim();
          const password = $('#of-pass', overlay).value;
          if (!username || (first && !password)) {
            showToast('Both a username and a password are needed.', { type: 'error' });
            return false;
          }
          post('/api/iptv-output/login', { username, password }).then((d) => {
            close();
            return refresh().then(() => showToast(d.first
              ? 'Login set. Turn the output on when you are ready.'
              : 'Login changed. Update it in each IPTV player.'));
          }).catch(fail);
          return false;
        } },
      ],
    });
  }

  // The Settings card: each row saves its own key through the Settings page's own route,
  // which is the one validator of these keys.
  async function saveSetting(el) {
    const path = el.dataset.settingPath;
    let value = el.value;
    if (el.type === 'number') {
      value = Math.round(Number(el.value));
      if (!Number.isFinite(value) || el.value.trim() === '') { refresh(); return; }
      if (el.min !== '') value = Math.max(Number(el.min), value);
      if (el.max !== '') value = Math.min(Number(el.max), value);
    }
    const mode = path.endsWith('.mode');
    try {
      await post(FIELD_URL, { path, value });
      el.value = value;
      await refresh(mode ? ALL : STATE_ONLY);
      if (mode) {
        const said = {
          proxy: 'Saved. Plays now go through ChannelBin.',
          mixed: 'Saved. Channel groups and sports channels now go through ChannelBin, and the rest straight to your IPTV provider.',
          redirect: 'Saved. An IPTV player is now sent straight to your IPTV provider.',
        };
        if (!(value in said)) throw new Error(`Unknown feed mode "${value}"`);
        showToast(said[value]);
      } else showToast(`Saved. ${el.dataset.label}: ${value}.`);
    } catch (err) {
      fail(err);
      refresh();   // the field goes back to what is stored
    }
  }

  const guarded = (fn) => (...args) => Promise.resolve(fn(...args)).catch(fail);

  document.addEventListener('click', (e) => {
    const t = e.target.closest('[data-act]');
    if (!t || t.tagName === 'INPUT' || t.tagName === 'SELECT') return;
    const act = t.dataset.act;
    if (act === 'copy') guarded(copyRow)(t);
    else if (act === 'reveal') guarded(revealAddress)(t);
    else if (act === 'login') openLogin(t);
    else if (act === 'power') guarded(setPower)(t.dataset.to);
    else if (act === 'stop') stopPlay(t);
    else if (act === 'live-now') goLiveNow(t).then((changed) => { if (changed) refresh(); });
    else if (act === 'channel-now') channelNow(t, e);
    else if (act === 'preview') previewFrom(t);
    else if (act === 'tab') { view.tab = t.dataset.tab; applyTab(); }
    else if (act === 'show') { view.show = t.dataset.show; view.page = 1; applyFilter(); }
    else if (act === 'page') { view.page += Number(t.dataset.dir); applyFilter(); }
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.act === 'row') setRow(t);
    else if (t.dataset.act === 'account') setAccount(t);
    else if (t.dataset.act === 'held') setHeld(t);
    else if (t.dataset.settingPath) saveSetting(t);
    else if (t.id === 'of-cat') { view.cat = t.value; view.page = 1; applyFilter(); }
  });
  document.addEventListener('input', (e) => {
    if (e.target.id !== 'of-q') return;
    view.q = e.target.value;
    view.page = 1;
    applyFilter();
  });

  // Recent plays' chips and pager are links, so a new-tab click works. A plain click puts the
  // link in the address bar and swaps only that card, through the same queue as every other
  // swap: the card has one updater, and a timed swap can never land after it with the old
  // filter. (bindHistoryChips swaps outside any queue, which is why it is not used here.)
  document.addEventListener('click', (e) => {
    const link = e.target.closest('#of-recent a[data-hist], #of-recent a[data-hist-more]');
    if (!link || wantsNewTab(e)) return;
    e.preventDefault();
    window.history.replaceState(null, '', link.href);
    swap(['#of-recent']).catch(() => window.location.reload());
  });
  setInterval(liveTick, LIVE_MS);
  document.addEventListener('visibilitychange', liveTick);
  applyView();
})();
