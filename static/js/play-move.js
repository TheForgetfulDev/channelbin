/* Watching now's "Move to another channel" (app/output_feed.py::move_by_hand, dev/changelog/1675):
   the one handler for the dashboard and the IPTV Output page, loaded by those two pages only. The
   element carries the play (data-id), the player (data-client), what it asked for (data-name) and
   the channel it is on (data-channel). Resolves true when the request was made, whatever came of
   it - a refused move leaves the play as it was, an ended one is gone - so the page redraws its
   Watching now either way. */
async function movePlay(el) {
  const d = el.dataset;
  const ok = await confirmModal({
    title: 'Move this play to another channel?',
    message: `The IPTV player at ${d.client} moves off "${d.channel}" to the next best channel that plays ${d.name}.`,
    consequence: `"${d.channel}" gets a score adjustment of -100, so it is tried last, for a play or a recording, until you set it back on its channel page.`,
    confirmLabel: 'Move',
  });
  if (!ok) return false;
  try {
    const r = await jsonFetch(`/api/iptv-output/plays/${encodeURIComponent(d.id)}/move`, { method: 'POST' });
    showToast(r.message, { type: r.warning ? 'warning' : 'success' });
  } catch (err) {
    showToast(err.message, { type: 'error' });
  }
  return true;
}
