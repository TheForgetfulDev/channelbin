/* Channel it would use on a sports game (app/sports_view.py::channel_now, dev/changelog/1639): the
   one handler for the league page's game rows and the IPTV Output page's sports rows, loaded by
   those two pages only. The element names the game (data-event), or why there is nothing to show
   yet (data-why). The answer is asked for on the click, worked out from the event id alone: with
   one answer to give it opens that channel and says so there, otherwise a dialog shows what a
   recording and a sports channel would each use, and why. A click asking for a new tab gets the
   dialog too: a tab opened after the fetch can be blocked as a popup, and the sentence left for
   the next page load would land on this tab instead. */
async function channelNow(el, e) {
  const d = el.dataset;
  if (d.why) { showToast(d.why, { type: 'warning' }); return; }
  let a;
  try {
    a = await jsonFetch(`/api/sports/events/${d.event}/channel-now`);
  } catch (err) {
    showToast(err.message, { type: 'error' });
    return;
  }
  if (a.go && !wantsNewTab(e)) {
    showToastAfterLoad(a.toast);
    followHref(e, a.go);
    return;
  }
  const { record, play } = a;
  if (!record.channel && !play.channel) {
    showToast([record.why, play.why || play.note].filter(Boolean).join(' '), { type: 'warning' });
    return;
  }
  const where = (side) => {
    if (!side.channel) return '';
    const c = side.channel;
    const group = side.group
      ? ` <span class="sp-why">of the group <a class="name-link" href="${escHtml(side.group.url)}">${escHtml(side.group.name)}</a></span>` : '';
    return `<div class="sp-chan"><span class="sp-cname"><a class="name-link" href="${escHtml(c.url)}">${escHtml(c.name)}</a> ${accountPill(c.account)}${group}</span><span class="sp-cact">${previewButton({ channelId: c.id, name: c.name })}</span></div>`;
  };
  const said = (side) => [side.why, side.note].filter(Boolean)
    .map((t) => `<p class="sp-why">${escHtml(t)}</p>`).join('');
  const body = `<div class="sp-dialog-body">
    <p class="sp-intro">If this ${escHtml(a.one)} started now, ${a.same ? 'a recording and its sports channel would both use the same channel.' : 'this is what a recording and its sports channel would each do.'}</p>
    <h3 class="sp-h3">A recording</h3>${where(record)}${said(record)}
    <h3 class="sp-h3">Its sports channel on the IPTV output</h3>${where(play)}${said(play)}
    ${a.footnote ? `<p class="sp-foot">${escHtml(a.footnote)}</p>` : ''}</div>`;
  buildModal({
    title: `${a.game}: channel it would use`,
    panelClass: 'sp-dialog',
    body,
    footer: [{ label: 'Close', class: 'btn' }],
  });
}
