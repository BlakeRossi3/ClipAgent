/* Theme: auto (system) / light / dark, remembered per browser. Loaded in <head> to avoid a flash. */
(function () {
  var KEY = 'dash-theme', root = document.documentElement;
  function get() { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } }
  function apply(t) { if (t) root.setAttribute('data-theme', t); else root.removeAttribute('data-theme'); }
  apply(get());
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-theme-toggle]');
    if (!b) return;
    var dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    var cur = get() || (dark ? 'dark' : 'light');
    var next = cur === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode */ }
    apply(next);
  });
})();
