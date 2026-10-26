/* jsdom lays nothing out, so Element.getClientRects() is always empty and
   util.js::overlayOpen() would call every open menu and dialog closed - a rig asserting
   that a page waits for one would pass for the wrong reason or fail for no reason.

   The stand-in: an element is rendered unless it or an ancestor is `hidden` or carries an
   inline display:none. That is how the hand-rolled dialogs (#record-modal) sit hidden in
   the DOM, which is the case a bare `.modal` match got wrong on the channel page
   (dev/docs/BUGS.md 2026-10-23 06:36 AM). Stylesheet-hidden elements read as rendered, so a rig
   that needs one closed uses the class the page closes it with. */
export function fakeLayout(window) {
  window.Element.prototype.getClientRects = function getClientRects() {
    for (let el = this; el && el.nodeType === 1; el = el.parentElement) {
      if (el.hidden || el.style.display === 'none') return [];
    }
    return [{}];
  };
}
