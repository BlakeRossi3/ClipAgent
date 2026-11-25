/* The top bar: dropdown menus (disclosure buttons) and the phone hamburger. Loaded in <head>; marks
   <html class="js"> at once so the no-JS fallback (menus open on hover / focus) switches off.
   Keyboard: Enter/Space/ArrowDown opens a menu and focuses its first item; ArrowUp/Down move inside;
   Home/End jump; ArrowLeft/Right move between top-level items; Escape closes and returns focus. */
(function () {
  'use strict';
  document.documentElement.classList.add('js');

  function init() {
    var bar = document.querySelector('[data-topbar]');
    if (!bar) return;
    var toggle = bar.querySelector('[data-nav-toggle]');
    var menus = Array.prototype.slice.call(bar.querySelectorAll('[data-menu]'));
    var tops = Array.prototype.slice.call(bar.querySelectorAll('.mainnav-list > li > .mainnav-link'));

    function items(li) { return Array.prototype.slice.call(li.querySelectorAll('[data-menu-list] a')); }
    function btn(li) { return li.querySelector('[data-menu-btn]'); }
    function isOpen(li) { return li.classList.contains('is-open'); }
    function close(li, focusBtn) {
      if (!isOpen(li)) return;
      li.classList.remove('is-open');
      btn(li).setAttribute('aria-expanded', 'false');
      if (focusBtn) btn(li).focus();
    }
    function closeAll(except) { menus.forEach(function (li) { if (li !== except) close(li); }); }
    function open(li, focusFirst) {
      closeAll(li);
      li.classList.add('is-open');
      btn(li).setAttribute('aria-expanded', 'true');
      if (focusFirst !== undefined) {
        var list = items(li);
        if (list.length) (focusFirst === 'last' ? list[list.length - 1] : list[0]).focus();
      }
    }

    menus.forEach(function (li) {
      var b = btn(li);
      b.addEventListener('click', function () { if (isOpen(li)) close(li); else open(li); });
      b.addEventListener('keydown', function (e) {
        if (e.key === 'ArrowDown') { e.preventDefault(); open(li, 'first'); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); open(li, 'last'); }
      });
      li.addEventListener('keydown', function (e) {
        var list = items(li), i = list.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.preventDefault(); close(li, true); return; }
        if (i < 0) return;
        if (e.key === 'ArrowDown') { e.preventDefault(); list[(i + 1) % list.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); list[(i - 1 + list.length) % list.length].focus(); }
        else if (e.key === 'Home') { e.preventDefault(); list[0].focus(); }
        else if (e.key === 'End') { e.preventDefault(); list[list.length - 1].focus(); }
        else if (e.key === 'Tab' && !e.shiftKey && i === list.length - 1) { close(li); }
      });
    });
    /* left/right between the top-level items (desktop bar) */
    tops.forEach(function (t, i) {
      t.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        var next = tops[(i + (e.key === 'ArrowRight' ? 1 : -1) + tops.length) % tops.length];
        var wasOpen = menus.some(isOpen);
        closeAll();
        next.focus();
        if (wasOpen && next.hasAttribute('data-menu-btn')) open(next.parentNode);
      });
    });
    document.addEventListener('click', function (e) { if (!bar.contains(e.target)) closeAll(); });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var o = menus.filter(isOpen)[0];
      if (o) close(o, true);
      else if (document.documentElement.classList.contains('nav-open') && toggle) { setNav(false); toggle.focus(); }
    });
    bar.addEventListener('focusout', function (e) {
      if (e.relatedTarget && !bar.contains(e.relatedTarget)) closeAll();
    });

    function setNav(on) {
      document.documentElement.classList.toggle('nav-open', on);
      if (toggle) toggle.setAttribute('aria-expanded', on ? 'true' : 'false');
      if (!on) closeAll();
    }
    if (toggle) toggle.addEventListener('click', function () { setNav(!document.documentElement.classList.contains('nav-open')); });
    /* phones: the menu of the current section starts unfolded */
    var cur = bar.querySelector('.has-menu > .is-current');
    if (toggle) toggle.addEventListener('click', function () {
      if (cur && document.documentElement.classList.contains('nav-open') && window.matchMedia('(max-width: 980px)').matches) open(cur.parentNode);
    });
  }
  /* "Update available": the server checks git at most every 10 minutes (cached); the chip posts to
     /setup/action/update (the same one-click update as Settings). */
  function checkUpdate() {
    var chip = document.querySelector('[data-update-chip]');
    if (!chip || !window.fetch) return;
    fetch('/dashboard/api/update', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.available) return;
        var label = chip.querySelector('[data-update-label]');
        if (label) label.textContent = j.label || 'Update available';
        chip.hidden = false;
      })
      .catch(function () {});
  }
  /* Live reload after a change: <html data-build> is the build this page was rendered from; the server's
     /dashboard/api/build changes when the app restarts (a self-edit or update) or a template / static file
     changes. Then the page reloads itself - but never under his hands: not while a field is focused, a
     form has unsaved typing, or the voice panel is listening, thinking or speaking. Typed-but-unsent text
     gets a "Reload" chip instead. A failed poll (the app mid-restart) just tries again. */
  var dirty = false;
  document.addEventListener('input', function (e) {
    var t = e.target;
    if (t && t.matches && t.matches('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, [contenteditable]')) dirty = true;
  }, true);
  document.addEventListener('submit', function () { dirty = false; }, true);
  function busy() {
    var a = document.activeElement;
    if (a && a !== document.body && a.matches && a.matches('input, textarea, select, [contenteditable]')) return true;
    if (document.querySelector('dialog[open], [data-state="thinking"], [data-state="speaking"], .is-live, .is-down')) return true;
    return false;
  }
  function offerReload() {
    if (document.querySelector('[data-reload-chip]')) return;
    var host = document.querySelector('[data-topbar] .topbar-inner') || document.body;
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'topbar-chip topbar-chip-update';
    b.setAttribute('data-reload-chip', '');
    b.title = 'This page changed: reload to see the new version';
    b.textContent = 'Updated · Reload';
    b.addEventListener('click', function () { window.location.reload(); });
    host.appendChild(b);
  }
  function watchBuild() {
    var mine = document.documentElement.getAttribute('data-build');
    if (!mine || !window.fetch) return;
    var changed = false;
    function tick() {
      if (changed) {
        if (dirty) { offerReload(); return; }
        if (!busy() && document.visibilityState !== 'hidden') { window.location.reload(); return; }
        setTimeout(tick, 3000);
        return;
      }
      fetch('/dashboard/api/build', { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (j && j.build && j.build !== mine) changed = true;
          setTimeout(tick, changed ? 0 : 10000);
        })
        .catch(function () { setTimeout(tick, 5000); });
    }
    setTimeout(tick, 10000);
    document.addEventListener('visibilitychange', function () {
      if (changed && document.visibilityState === 'visible' && !dirty && !busy()) window.location.reload();
    });
  }
  function boot() { init(); checkUpdate(); watchBuild(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
