/* Go live now on a sports game (app/sports_feed.py::set_live_now, dev/changelog/1530): the one
   handler for the IPTV Output page's sports rows and the league page's game rows, loaded by those
   two pages only. The element names the game (data-event) and carries what the server worked out
   for it: data-on, the confirm's text (data-message) and the sports channels it plays on
   (data-channels), or why there is nothing to open (data-why). Resolves true when the page should
   swap itself from the server: after a change, and after a refusal, since what it showed was
   out of date. */
async function goLiveNow(el) {
  const d = el.dataset;
  if (d.why) { showToast(d.why, { type: 'warning' }); return false; }
  const turnOn = d.on !== '1';
  if (turnOn) {
    const ok = await confirmModal({
      title: 'Go live now',
      message: d.message,
      list: JSON.parse(d.channels || '[]'),
      confirmLabel: 'Go live now',
    });
    if (!ok) return false;
  }
  try {
    const answer = await jsonFetch(`/api/sports/events/${d.event}/live-now`,
      { method: 'PUT', body: JSON.stringify({ on: turnOn }) });
    showToast(answer.message);
  } catch (err) {
    showToast(err.message, { type: 'error' });
  }
  return true;
}
