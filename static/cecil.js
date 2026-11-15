/* The Cecil home around the ring: the live status strip, the "working on it" pill, orbiting dots on the
   ring (one per running step / background task / busy specialist) and the panels (recent chats, staff
   at work, recently worked on, new stuff, charts) refreshed from GET /dashboard/api/cecil.
   It never touches the voice logic: it only READS what static/jarvis.js renders (the root's
   data-state, the rail's [data-jv-steps] / [data-jv-tasks]) and swaps server-rendered, autoescaped
   fragments into [data-cc-live] boxes. Text it builds itself goes through textContent. */
(function () {
  'use strict';
  var root = document.querySelector('[data-jarvis]');
  if (!root) return;
  function q(s) { return document.querySelector(s); }
  var stageEl = q('[data-cc-stage]'), elapsedEl = q('[data-cc-elapsed]'), workingEl = q('[data-cc-working]'),
    workingText = q('[data-cc-working-text]'), orbits = q('[data-cc-orbits]'), brainEl = q('[data-cc-brain]'),
    workingN = q('[data-cc-working-n]'), tasksN = q('[data-cc-tasks-n]');
  var stepsEl = q('[data-jv-steps]'), tasksEl = q('[data-jv-tasks]');
  var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var staffBusy = 0, busySince = 0, lastDur = 0, wasBusy = false, tick = null;
  var STAGES = { idle: 'Standing by', listening: 'Listening', thinking: 'Thinking', speaking: 'Speaking',
    muted: 'Muted', error: 'Something went wrong' };

  function runningSteps() {
    if (!stepsEl) return [];
    var all = stepsEl.querySelectorAll('.rail-step'), out = [];
    for (var i = 0; i < all.length - 1; i++) {       // the last row is the turn's own status line
      if (all[i].classList.contains('is-running')) {
        var spans = all[i].querySelectorAll('span'), label = spans.length ? spans[spans.length - 1].textContent : '';
        out.push(label);
      }
    }
    return out;
  }
  function runningTasks() { return tasksEl ? tasksEl.querySelectorAll('.rail-task.is-running').length : 0; }
  function fmt(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
  }
  function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }

  function drawOrbits(n) {
    if (!orbits) return;
    n = Math.min(8, n);
    if (orbits.childElementCount === n) return;
    orbits.textContent = '';
    for (var i = 0; i < n; i++) {
      var d = document.createElement('span');
      d.className = 'cc-orbit';
      d.style.setProperty('--i', i);
      d.style.setProperty('--n', n);
      d.style.setProperty('--r', (40 + (i % 3) * 5) + '%');
      d.style.setProperty('--t', (5 + (i % 4) * 1.6) + 's');
      orbits.appendChild(d);
    }
  }

  function update() {
    var st = root.getAttribute('data-state') || 'idle', steps = runningSteps(), tasks = runningTasks();
    var busy = st === 'thinking' || st === 'speaking' || steps.length > 0;
    var now = Date.now();
    if (busy && !wasBusy) busySince = now;
    if (!busy && wasBusy) { lastDur = now - busySince; setTimeout(refresh, 1200); }
    wasBusy = busy;
    var stage = STAGES[st] || 'Standing by';
    if (st === 'thinking' && steps.length) stage = 'Using a tool: ' + steps[steps.length - 1];
    setText(stageEl, stage);
    if (stageEl) stageEl.title = stage;
    setText(elapsedEl, busy ? fmt(now - busySince) : (lastDur ? 'last ' + fmt(lastDur) : '0:00'));
    var jobs = steps.length + tasks + staffBusy, working = busy || tasks > 0 || staffBusy > 0;
    root.classList.toggle('cc-busy', working);
    if (workingEl) {
      workingEl.hidden = !working;
      var bits = ['Working on it'];
      if (st === 'thinking' && steps.length) bits.push(steps[steps.length - 1]);
      if (tasks) bits.push(tasks + ' background task' + (tasks === 1 ? '' : 's'));
      if (staffBusy && !steps.length) bits.push(staffBusy + ' specialist' + (staffBusy === 1 ? '' : 's') + ' busy');
      setText(workingText, bits.join(' · '));
    }
    setText(tasksN, String(tasks));
    drawOrbits(working ? Math.max(1, jobs) : 0);
    if (busy && !tick) tick = setInterval(update, 1000);
    if (!busy && tick) { clearInterval(tick); tick = null; }
  }

  /* the panels: server-rendered (escaped) fragments, swapped in place */
  var inflight = false, timer = null;
  function refresh() {
    if (inflight || document.hidden || !window.fetch) return;
    inflight = true;
    fetch('/dashboard/api/cecil', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!j) return;
        var html = j.html || {};
        Object.keys(html).forEach(function (k) {
          var box = document.querySelector('[data-cc-live="' + k + '"]');
          if (box && typeof html[k] === 'string' && box.getAttribute('data-sig') !== String(html[k].length) + html[k].slice(-40)) {
            box.innerHTML = html[k];          // rendered by Jinja with autoescape (home_live.render_parts)
            box.setAttribute('data-sig', String(html[k].length) + html[k].slice(-40));
          }
        });
        if (brainEl && j.brain) {
          brainEl.setAttribute('data-brain', String(j.brain.state || 'unknown'));
          var b = brainEl.querySelector('b'); setText(b, String(j.brain.label || ''));
        }
        staffBusy = (j.staff || []).filter(function (x) { return x.state === 'working' && x.name !== 'main'; }).length;
        setText(workingN, String(j.working || 0));
        update();
      })
      .catch(function () { /* next tick */ })
      .then(function () { inflight = false; });
  }
  function start() { if (!timer) timer = setInterval(refresh, 15000); }
  function stop() { clearInterval(timer); timer = null; }
  document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); else { refresh(); start(); } });

  /* "All" on Recent chats opens the history drawer (jarvis.js owns it) */
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-jv-history-link]');
    if (!a) return;
    var b = q('[data-jv-history]');
    if (b && document.documentElement.classList.contains('jv-on')) { e.preventDefault(); b.click(); }
  });

  if (window.MutationObserver) {
    var mo = new MutationObserver(update);
    mo.observe(root, { attributes: true, attributeFilter: ['data-state'] });
    if (stepsEl) mo.observe(stepsEl, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    if (tasksEl) mo.observe(tasksEl, { childList: true, subtree: true });
  }
  staffBusy = document.querySelectorAll('[data-cc-live="staff"] .cc-agent.is-working:not(:first-child)').length;
  update();
  start();
  if (reduced) root.classList.add('cc-reduced');
})();
