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
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
