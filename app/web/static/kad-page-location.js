/* Store helper 0.3.0 requires a fragment-free service document URL. */
(() => {
  function normalizeAnchor() {
    if (!location.hash) return;
    const target = location.hash.slice(1);
    // Preserve registry/filter query and history state without navigating or losing edits.
    history.replaceState(history.state, '', location.pathname + location.search);
    const scroll = () => document.getElementById(target)?.scrollIntoView();
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', scroll, {once: true});
    } else {
      scroll();
    }
  }
  normalizeAnchor();
  window.addEventListener('hashchange', normalizeAnchor);
})();
