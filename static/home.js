/* Command-centre home: live refresh, chat, optimistic forms, agent sheets. No framework, no external requests.
   Everything inserted from user/model text goes through textContent; only server-rendered (escaped)
   fragments from /dashboard/api/home are placed with innerHTML. */
(function () {
  'use strict';
  var home = document.querySelector('.home[data-refresh]');

  function postForm(form) {
    return fetch(form.action, {
      method: 'POST', body: new FormData(form), credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; });
    });
  }

  /* ---- agent sheets ---------------------------------------------------- */
  document.addEventListener('click', function (ev) {
    var a = ev.target.closest && ev.target.closest('a[data-sheet]');
    if (!a) return;
    var d = document.getElementById(a.getAttribute('data-sheet'));
    if (d && typeof d.showModal === 'function') { ev.preventDefault(); d.showModal(); }
  });
  document.addEventListener('click', function (ev) {
    if (ev.target.tagName === 'DIALOG') ev.target.close();   // tap on the backdrop closes
  });

  /* ---- optimistic forms: meals + check-in answers ------------------------- */
  document.addEventListener('submit', function (ev) {
    var form = ev.target;
    var kind = form.getAttribute('data-async');
    if (!kind || !window.fetch) return;
    ev.preventDefault();
    var item = form.closest('[data-meal], .need');
    var prev = item ? item.className : '';
    if (kind === 'meal' && item) {
      var accept = /accept$/.test(form.action);
      item.classList.add(accept ? 'meal-accepted' : 'meal-declined');
      var acts = item.querySelector('.meal-actions');
      if (acts) { acts.dataset.prev = acts.innerHTML; acts.textContent = accept ? 'accepted' : 'declined'; acts.className = 'meal-actions meal-state'; }
    } else if (item) {
      item.classList.add('is-done');
    }
    postForm(form).then(function (j) {
      if (!j.ok) throw new Error(j.msg || 'failed');
      if (kind === 'answer' && item) {
        var p = document.createElement('p');
        p.className = 'need-thanks';
        p.textContent = j.logged ? ('Noted: ' + j.logged.hours + ' h at ' + j.logged.job + '.') : 'Thank you, noted.';
        form.replaceWith(p);
      }
    }).catch(function () {
      if (item) item.className = prev;
      var acts = item && item.querySelector('.meal-actions');
      if (acts && acts.dataset.prev) { acts.innerHTML = acts.dataset.prev; acts.className = 'meal-actions'; }
      form.classList.add('is-error');
    });
  });

  /* ---- chat --------------------------------------------------------------- */
  var chat = document.querySelector('form[data-chat]');
  var log = document.querySelector('[data-chat-log]');
  function bubble(who, text, meta) {
    var empty = log.querySelector('[data-empty]');
    if (empty) empty.remove();
    var li = document.createElement('li');
    li.className = 'msg msg-' + who;
    var b = document.createElement('p'); b.className = 'msg-body'; b.textContent = text; li.appendChild(b);
    if (meta) { var m = document.createElement('p'); m.className = 'msg-meta'; m.textContent = meta; li.appendChild(m); }
    log.appendChild(li);
    log.scrollTop = log.scrollHeight;
    return li;
  }
  if (chat && log) {
    log.scrollTop = log.scrollHeight;
    var ta = chat.querySelector('textarea');
    ta.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); chat.requestSubmit ? chat.requestSubmit() : chat.submit(); }
    });
    ta.addEventListener('input', function () { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 140) + 'px'; });
    chat.addEventListener('submit', function (ev) {
      if (!window.fetch) return;
      ev.preventDefault();
      var text = ta.value.trim();
      if (!text) return;
      bubble('me', text, 'dashboard · just now');
      var wait = bubble('them', '', '');
      wait.classList.add('is-typing');
      wait.querySelector('.msg-body').innerHTML = '<i></i><i></i><i></i>';
      var fd = new FormData(chat);
      ta.value = ''; ta.style.height = '';
      var btn = chat.querySelector('button'); btn.disabled = true;
      fetch(chat.action, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json().catch(function () { return {}; }); })
        .then(function (j) {
          wait.remove();
          if (j.ok) bubble('them', j.reply, 'dashboard · just now · ' + (j.fake ? 'practice' : j.cost_text));
          else bubble('them', j.reason || j.msg || 'That did not go through. Try again in a moment.', 'not sent');
        })
        .catch(function () { wait.remove(); bubble('them', 'The connection dropped. Try again in a moment.', 'not sent'); })
        .then(function () { btn.disabled = false; ta.focus(); });
    });
    document.addEventListener('keydown', function (ev) {
      var t = ev.target.tagName;
      if (ev.key === '/' && t !== 'INPUT' && t !== 'TEXTAREA' && !ev.metaKey && !ev.ctrlKey && !ta.disabled) {
        ev.preventDefault(); ta.focus();
      }
    });
  }

  /* ---- live refresh ------------------------------------------------------- */
  if (!home || !window.fetch) return;
  var every = Math.max(10, +home.getAttribute('data-refresh') || 25) * 1000, timer = null;
  function refresh() {
    if (document.hidden || document.querySelector('dialog[open]')) return;
    var active = document.activeElement;
    if (active && active.closest && active.closest('[data-live]') && /INPUT|TEXTAREA/.test(active.tagName)) return;
    fetch('/dashboard/api/home', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j || !j.html) return;
        Object.keys(j.html).forEach(function (k) {
          var box = document.querySelector('[data-live="' + k + '"]');
          if (!box) return;
          var open = Array.prototype.map.call(box.querySelectorAll('details[open][id]'), function (d) { return d.id; });
          box.innerHTML = j.html[k];
          open.forEach(function (id) { var d = document.getElementById(id); if (d) d.open = true; });
        });
        var hero = document.getElementById('hero');
        if (hero) hero.className = 'hero mood-' + j.mood;
        ['salute', 'line'].forEach(function (k) {
          var el = document.querySelector('[data-live-text="' + k + '"]');
          if (el && j[k]) el.textContent = j[k];
        });
      }).catch(function () { /* offline: try next tick */ });
  }
  function start() { if (!timer) timer = setInterval(refresh, every); }
  function stop() { clearInterval(timer); timer = null; }
  document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); else { refresh(); start(); } });
  start();
})();
