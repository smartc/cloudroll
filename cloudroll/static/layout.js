/* Which layout (large image) this browser shows.  The choice is per browser;
   until one is made, the server's dashboard default applies. */
const CRLayout = (() => {
  const KEY = 'cloudroll.layout';
  const cfg = window.CR_LAYOUTS || {layouts: [], default: null};
  const listeners = [];

  function get() {
    let v = null;
    try { v = localStorage.getItem(KEY); } catch (e) { /* storage blocked */ }
    return cfg.layouts.includes(v) ? v : cfg.default;
  }

  function mark(v) {
    document.querySelectorAll('.layout-switch [data-layout]').forEach(b =>
      b.classList.toggle('act', b.dataset.layout === v));
  }

  function set(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* storage blocked */ }
    mark(v);
    listeners.forEach(f => f(v));
  }

  /* Append ?layout= (or &layout=) to a URL for the current choice. */
  function url(u, v = get()) {
    if (!v) return u;
    return u + (u.includes('?') ? '&' : '?') + 'layout=' + encodeURIComponent(v);
  }

  document.addEventListener('DOMContentLoaded', () => {
    mark(get());
    document.querySelectorAll('.layout-switch [data-layout]').forEach(b =>
      b.addEventListener('click', () => set(b.dataset.layout)));
  });

  return {get, set, url, onChange: f => listeners.push(f)};
})();
