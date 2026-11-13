/* Jarvis view: the voice-first home stage (templates/home.html).
   - An animated core drawn on a canvas (original art) whose state follows this tab's microphone and
     the laptop's background voice loop (/voice/api/status).
   - In-browser voice: hold the ring (or Space) to talk, or "Always listen in this tab" (energy VAD).
     The clip goes to POST /voice/api/talk; the answer is polled at /dashboard/chat/job/{id} and its
     audio (synthesised on the server) is played through Web Audio so the core can pulse with it.
   - History drawer + mute. Look and feel adapted from adewaskar/jarvis src/ui/Hud.tsx and index.css
     (MIT License, Copyright (c) 2026 Aditya Dewaskar).
   No framework, no external requests. Every user/model string is inserted with textContent. */
(function () {
  'use strict';
  var root = document.querySelector('[data-jarvis]');
  if (!root) return;
  document.documentElement.classList.add('jv-on');
  function q(s) { return root.querySelector(s); }
  var canvas = q('[data-jv-core]'), ptt = q('[data-jv-ptt]'), statusEl = q('[data-jv-status]'),
    youEl = q('[data-jv-you]'), themEl = q('[data-jv-them]'), hintEl = q('[data-jv-hint]'), sysEl = q('[data-jv-sys]'),
    alwaysEl = q('[data-jv-always]'), muteBtn = q('[data-jv-mute]'), histBtn = q('[data-jv-history]');
  var drawer = document.querySelector('[data-jv-drawer]'), scrim = document.querySelector('[data-jv-scrim]');
  var logEl = document.querySelector('[data-chat-log]');
  var csrf = root.getAttribute('data-csrf'), H = root.getAttribute('data-honorific') || 'sir';
  var canTalk = root.getAttribute('data-can-talk') === '1';
  var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var serverStatus = statusEl.textContent;

  /* ---- state ---------------------------------------------------------------------------- */
  var local = null;          // listening | thinking | speaking | error: this tab is busy
  var bg = { state: 'offline', online: false, muted: false };
  var tabMuted = false, always = false, errText = '', errTimer = null, note = '', noteTimer = null;
  var BG = { idle: 'idle', followup: 'listening', listening: 'listening', thinking: 'thinking', speaking: 'speaking',
    muted: 'muted', no_mic: 'idle', starting: 'idle', offline: 'idle', stopped: 'idle' };
  function visual() {
    if (local) return local;
    if (tabMuted || bg.muted) return 'muted';
    return BG[bg.state] || 'idle';
  }
  function render() {
    var v = visual(), text;
    root.setAttribute('data-state', v);
    if (!canTalk) text = serverStatus;
    else if (v === 'error') text = errText;
    else if (note && v === 'idle') text = note;
    else if (v === 'listening') text = 'Listening, ' + H;
    else if (v === 'thinking') text = 'Thinking…';
    else if (v === 'speaking') text = 'Speaking';
    else if (v === 'muted') text = 'Muted — tap Unmute to talk';
    else if (always && stream) text = 'Listening for you in this tab, ' + H;
    else text = 'Standing by, ' + H;
    if (statusEl.textContent !== text) statusEl.textContent = text;
    var sys = bg.online ? (bg.muted ? 'laptop mic muted' : (bg.state === 'no_mic' ? 'laptop mic missing' : 'laptop mic live'))
      : 'laptop voice off';
    if (always && stream) sys += ' · tab mic on';
    sysEl.textContent = sys;
    muteBtn.textContent = (tabMuted || bg.muted) ? 'Unmute' : 'Mute';
    muteBtn.setAttribute('aria-pressed', (tabMuted || bg.muted) ? 'true' : 'false');
    ptt.classList.toggle('is-live', local === 'listening');
  }
  function fail(msg) {
    errText = msg; local = 'error'; render();
    clearTimeout(errTimer);
    errTimer = setTimeout(function () { if (local === 'error') { local = null; render(); } }, 7000);
  }
  function flash(msg) {
    note = msg; render(); clearTimeout(noteTimer);
    noteTimer = setTimeout(function () { note = ''; render(); }, 3500);
  }

  /* ---- audio graph ------------------------------------------------------------------------ */
  var ctx = null, outAnalyser = null, micAnalyser = null, stream = null, micReady = null, releaseTimer = null;
  var tbuf = new Float32Array(1024), fbuf = new Uint8Array(512);
  function audioCtx() {
    if (!ctx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      ctx = new AC();
      outAnalyser = ctx.createAnalyser(); outAnalyser.fftSize = 1024; outAnalyser.smoothingTimeConstant = 0.55;
      outAnalyser.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  function ensureMic() {
    clearTimeout(releaseTimer);
    if (micReady) return micReady;
    if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return Promise.reject({ name: 'Insecure' });
    }
    micReady = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(function (s) {
        stream = s;
        var c = audioCtx();
        if (c) {
          micAnalyser = c.createAnalyser(); micAnalyser.fftSize = 1024; micAnalyser.smoothingTimeConstant = 0.5;
          c.createMediaStreamSource(s).connect(micAnalyser);
        }
        s.getAudioTracks().forEach(function (t) { t.addEventListener('ended', releaseMic); });
        render();
        return s;
      }, function (e) { micReady = null; throw e; });
    return micReady;
  }
  function releaseMic() {
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    stream = null; micReady = null; micAnalyser = null; render();
  }
  function releaseSoon() {   // the browser's red mic dot goes away when nobody is using it
    clearTimeout(releaseTimer);
    releaseTimer = setTimeout(function () { if (!always && !rec) releaseMic(); }, 60000);
  }
  function micError(e) {
    var n = e && e.name;
    if (n === 'NotAllowedError' || n === 'SecurityError') fail('Microphone blocked. Allow it for this site (the icon left of the address), then try again.');
    else if (n === 'NotFoundError' || n === 'OverconstrainedError') fail('No microphone found on this device.');
    else if (n === 'NotReadableError') fail('The microphone is in use by another app.');
    else if (n === 'Insecure') fail('Browsers only allow the microphone on https:// or http://127.0.0.1 — open the dashboard that way.');
    else fail('Could not open the microphone.');
    always = false; if (alwaysEl) alwaysEl.checked = false;
  }
  function rms(an) {
    if (!an) return 0;
    an.getFloatTimeDomainData(tbuf);
    var s = 0;
    for (var i = 0; i < tbuf.length; i++) s += tbuf[i] * tbuf[i];
    return Math.sqrt(s / tbuf.length);
  }

  /* ---- recording ------------------------------------------------------------------------- */
  var rec = null, recMode = '', recStart = 0, maxTimer = null, pttDown = false, discard = false, turn = 0;
  function mimeType() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    for (var i = 0; i < types.length; i++) if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    return '';
  }
  function startRec(mode) {
    if (!canTalk || rec) return;
    stopPlayback();                       // barge-in: talking again stops the reply
    if (tabMuted) { tabMuted = false; }
    audioCtx();
    ensureMic().then(function (s) {
      if (rec || (mode === 'ptt' && !pttDown)) return;   // released before the mic opened
      if (!window.MediaRecorder) { fail('This browser cannot record audio. Type in History instead.'); return; }
      var type = mimeType(), chunks = [];
      try { rec = type ? new MediaRecorder(s, { mimeType: type }) : new MediaRecorder(s); } catch (e) { fail('This browser cannot record audio.'); return; }
      recMode = mode; recStart = performance.now(); discard = false;
      rec.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
      rec.onstop = function () {
        var r = rec, dur = performance.now() - recStart; rec = null;
        var blob = new Blob(chunks, { type: (r && r.mimeType) || type || 'audio/webm' });
        if (!always) releaseSoon();
        if (discard) { local = null; render(); return; }
        if (dur < 450 || blob.size < 600) { local = null; flash('Hold the ring while you speak, ' + H + '.'); return; }
        send(blob);
      };
      rec.start(250);
      local = 'listening'; render();
      clearTimeout(maxTimer); maxTimer = setTimeout(function () { stopRec(); }, 30000);
    }, micError);
  }
  function stopRec(drop) {
    clearTimeout(maxTimer);
    if (rec && rec.state !== 'inactive') { discard = !!drop; rec.stop(); }
  }

  function getJSON(r) { return r.json().catch(function () { return {}; }); }
  function send(blob) {
    var my = ++turn;
    local = 'thinking'; youEl.textContent = '…'; render();
    var fd = new FormData();
    fd.append('csrf', csrf);
    fd.append('audio', blob, 'speech.' + (/mp4/.test(blob.type) ? 'm4a' : /ogg/.test(blob.type) ? 'ogg' : 'webm'));
    fetch('/voice/api/talk', { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(getJSON).then(function (j) {
        if (my !== turn) return;
        if (!j.ok) {
          youEl.textContent = '';
          if (j.error === 'no_speech' || j.error === 'too_short') { local = null; flash(j.msg); }
          else fail(j.msg || j.reason || 'That did not go through.');
          return;
        }
        youEl.textContent = j.heard;
        addHistory('me', j.heard, 'voice · this tab');
        if (j.command) { command(j.command); return; }
        poll(j.job, my, 400);
      }).catch(function () { if (my === turn) fail('The connection dropped. Try again.'); });
  }
  function poll(job, my, tries) {
    setTimeout(function () {
      if (my !== turn) return;
      fetch('/dashboard/chat/job/' + job, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(getJSON).then(function (j) {
          if (my !== turn) return;
          if (!j.done) { if (tries > 0) poll(job, my, tries - 1); else fail('Still working; the answer will land in History.'); return; }
          if (!j.ok) { fail(j.msg || 'That did not go through.'); return; }
          caption(j.reply || '');
          addHistory('them', j.reply || '', 'voice · just now');
          if (j.audio) play(j.audio, my); else { local = null; render(); }
        }).catch(function () { if (tries > 0) poll(job, my, tries - 1); });
    }, 900);
  }
  function command(cmd) {
    if (cmd === 'mute') { setMuted(true); caption('Microphone off, ' + H + '.'); }
    else { stopPlayback(); local = null; caption('Very good, ' + H + '.'); render(); }
  }

  /* ---- playback -------------------------------------------------------------------------- */
  var src = null;
  function play(url, my) {
    var c = audioCtx();
    if (!c) { local = null; render(); return; }
    fetch(url, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('audio'); return r.arrayBuffer(); })
      .then(function (ab) { return new Promise(function (res, rej) { c.decodeAudioData(ab, res, rej); }); })
      .then(function (buffer) {
        if (my !== turn) return;
        stopPlayback();
        var s = c.createBufferSource();
        s.buffer = buffer; s.connect(outAnalyser);
        s.onended = function () { if (src === s) { src = null; if (local === 'speaking') { local = null; render(); } } };
        src = s; s.start();
        local = 'speaking'; render();
      }).catch(function () { if (my === turn) { local = null; render(); } });
  }
  function stopPlayback() {
    if (!src) return;
    var s = src; src = null;
    try { s.stop(); } catch (e) { /* already stopped */ }
    if (local === 'speaking') local = null;
  }

  /* ---- captions (the reply resolves out of glyph noise, as in adewaskar/jarvis DecodeText) ---- */
  var GLYPHS = '/\\|<>[]{}=+*#%&0123456789ABCDEFGHJKLMNPQRSTUVWXYZ', capTimer = null;
  function caption(text) {
    clearInterval(capTimer);
    if (reduced || !text) { themEl.textContent = text; return; }
    var shown = 0, rate = Math.max(3, Math.ceil(text.length / 30));
    capTimer = setInterval(function () {
      shown = Math.min(text.length, shown + rate);
      var noise = '';
      for (var i = shown; i < Math.min(text.length, shown + 14); i++) {
        noise += text[i] === ' ' ? ' ' : GLYPHS[(i * 7919 + shown * 31) % GLYPHS.length];
      }
      themEl.textContent = text.slice(0, shown) + noise;
      if (shown >= text.length) clearInterval(capTimer);
    }, 33);
  }
  function addHistory(who, text, meta) {
    if (!logEl) return;
    var empty = logEl.querySelector('[data-empty]');
    if (empty) empty.remove();
    var li = document.createElement('li');
    li.className = 'msg msg-' + who;
    var b = document.createElement('p'); b.className = 'msg-body'; b.textContent = text; li.appendChild(b);
    var m = document.createElement('p'); m.className = 'msg-meta';
    var chip = document.createElement('span'); chip.className = 'chip chan-voice'; chip.textContent = 'voice';
    m.appendChild(chip); m.appendChild(document.createTextNode(' · ' + meta.replace(/^voice · /, '')));
    li.appendChild(m);
    logEl.appendChild(li);
    logEl.scrollTop = logEl.scrollHeight;
  }

  /* ---- always-listen (energy VAD with an adaptive noise floor) ---------------------------- */
  var vad = { floor: 0.01, on: 0, quiet: 0 };
  setInterval(function () {
    if (!always || !micAnalyser) return;
    var l = rms(micAnalyser);
    if (!rec) {
      if (local === 'thinking') return;
      vad.floor = l < vad.floor ? vad.floor * 0.7 + l * 0.3 : vad.floor * 0.995 + l * 0.005;
      var thr = Math.max(0.018, vad.floor * 3.2) * (src ? 2.6 : 1);   // higher while it is talking (echo)
      vad.on = l > thr ? vad.on + 1 : 0;
      if (vad.on >= 3) { vad.on = 0; vad.quiet = 0; startRec('vad'); }
    } else if (recMode === 'vad') {
      vad.quiet = l < Math.max(0.012, vad.floor * 2.2) ? vad.quiet + 50 : 0;
      if (vad.quiet >= 900) stopRec();
    }
  }, 50);
  if (alwaysEl) alwaysEl.addEventListener('change', function () {
    if (alwaysEl.checked) {
      audioCtx();
      ensureMic().then(function () { always = true; tabMuted = false; render(); }, micError);
    } else {
      always = false;
      if (recMode === 'vad') stopRec(true);
      releaseSoon(); render();
    }
  });

  /* ---- push-to-talk: hold the ring, or hold Space ------------------------------------------ */
  function press() { if (pttDown) return; pttDown = true; ptt.classList.add('is-down'); startRec('ptt'); }
  function release() {
    if (!pttDown) return;
    pttDown = false; ptt.classList.remove('is-down');
    if (rec && recMode === 'ptt') stopRec();
  }
  ptt.addEventListener('pointerdown', function (e) {
    if (e.button) return;
    e.preventDefault();
    if (ptt.setPointerCapture) { try { ptt.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ } }
    press();
  });
  ptt.addEventListener('pointerup', release);
  ptt.addEventListener('pointercancel', release);
  ptt.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  ptt.addEventListener('click', function (e) {      // keyboard Enter: toggle
    if (e.detail !== 0) return;
    if (rec) stopRec(); else startRec('toggle');
  });
  function typingIn(el) {
    if (!el || el === ptt) return false;
    return /^(INPUT|TEXTAREA|SELECT|BUTTON|A|SUMMARY)$/.test(el.tagName) || el.isContentEditable;
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { if (drawerOpen()) setDrawer(false); else if (rec) stopRec(true); else stopPlayback(); render(); return; }
    if (e.code !== 'Space' || e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target) || !canTalk) return;
    if (document.querySelector('dialog[open]')) return;
    e.preventDefault();
    if (!e.repeat) press();
  });
  document.addEventListener('keyup', function (e) { if (e.code === 'Space' && pttDown) { e.preventDefault(); release(); } });

  /* ---- mute (this tab + the laptop's voice loop) ---------------------------------------------- */
  function setMuted(on) {
    tabMuted = on;
    if (on) { always = false; if (alwaysEl) alwaysEl.checked = false; stopRec(true); stopPlayback(); releaseMic(); local = null; }
    var fd = new FormData(); fd.append('csrf', muteBtn.getAttribute('data-csrf')); fd.append('muted', on ? '1' : '0');
    fetch('/voice/api/mute', { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function () { bg.muted = on; render(); }).catch(function () { /* the tab mute still holds */ });
    render();
  }
  muteBtn.addEventListener('click', function () { setMuted(!(tabMuted || bg.muted)); });

  /* ---- history drawer ---------------------------------------------------------------------- */
  function drawerOpen() { return document.documentElement.classList.contains('jv-drawer-open'); }
  function setDrawer(open) {
    document.documentElement.classList.toggle('jv-drawer-open', open);
    if (drawer) drawer.inert = !open;
    if (scrim) scrim.hidden = !open;
    histBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && logEl) logEl.scrollTop = logEl.scrollHeight;
    if (!open) histBtn.focus({ preventScroll: true });
  }
  if (drawer) drawer.inert = true;
  histBtn.addEventListener('click', function () { setDrawer(!drawerOpen()); });
  if (scrim) scrim.addEventListener('click', function () { setDrawer(false); });
  var closeBtn = document.querySelector('[data-jv-close]');
  if (closeBtn) closeBtn.addEventListener('click', function () { setDrawer(false); });
  document.addEventListener('keydown', function (e) {   // "/" opens the drawer; home.js then focuses the box
    var t = e.target.tagName;
    if (e.key === '/' && t !== 'INPUT' && t !== 'TEXTAREA' && !e.metaKey && !e.ctrlKey && !drawerOpen()) setDrawer(true);
  });
  if (location.hash === '#history' || location.hash === '#talk') setDrawer(true);

  /* ---- the laptop's voice loop --------------------------------------------------------------- */
  var lastHeard = null, lastSaid = null;
  function pollStatus() {
    if (document.hidden) return;
    fetch('/voice/api/status', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j) return;
        bg = j;
        if (lastHeard !== null && !local) {        // captions for what the laptop loop heard and said
          if (j.heard && j.heard !== lastHeard) youEl.textContent = j.heard;
          if (j.said && j.said !== lastSaid) caption(j.said);
        }
        lastHeard = j.heard || ''; lastSaid = j.said || '';
        render();
      }).catch(function () { /* offline: next tick */ });
  }
  pollStatus();
  setInterval(pollStatus, 1500);

  /* ---- the core ------------------------------------------------------------------------------ */
  var g = canvas.getContext('2d');
  var COLORS = { idle: [0, 214, 240], listening: [120, 245, 255], thinking: [232, 176, 84], speaking: [0, 229, 255],
    muted: [96, 110, 122], error: [255, 92, 72] };
  var col = COLORS.idle.slice(), level = 0, spin = 0, last = 0, dpr = 1, size = 0, raf = 0;
  function fit() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    var r = canvas.getBoundingClientRect();
    size = Math.max(120, Math.round(Math.min(r.width, r.height) * dpr));
    if (canvas.width !== size) { canvas.width = size; canvas.height = size; }
  }
  function rgba(c, a) { return 'rgba(' + (c[0] | 0) + ',' + (c[1] | 0) + ',' + (c[2] | 0) + ',' + Math.max(0, Math.min(1, a)).toFixed(3) + ')'; }
  function noise(i, t) { return 0.5 + 0.25 * Math.sin(i * 12.9898 + t * 0.7) + 0.25 * Math.sin(i * 4.1414 - t * 1.3); }
  function spectrum(an) {
    if (!an) return null;
    an.getByteFrequencyData(fbuf);
    return fbuf;
  }
  function frame(now) {
    raf = 0;
    var dt = Math.min(0.1, (now - last) / 1000 || 0.016); last = now;
    var st = root.getAttribute('data-state'), t = reduced ? 0 : now / 1000;
    var target = COLORS[st] || COLORS.idle;
    for (var k = 0; k < 3; k++) col[k] += (target[k] - col[k]) * Math.min(1, dt * 4);
    var an = st === 'speaking' && src ? outAnalyser : (st === 'listening' && local ? micAnalyser : null);
    var want = an ? Math.min(1, rms(an) * 7) : st === 'listening' ? 0.18 + 0.12 * Math.sin(now / 160) : st === 'speaking' ? 0.3 + 0.25 * Math.abs(Math.sin(now / 110)) : 0;
    level += (want - level) * Math.min(1, dt * 12);
    var speed = { idle: 0.06, listening: 0.18, thinking: 1.1, speaking: 0.25, muted: 0.015, error: 0.03 }[st] || 0.06;
    if (!reduced) spin += dt * speed;
    var w = canvas.width, cx = w / 2, cy = w / 2, R = w * 0.3;
    var dim = st === 'muted' ? 0.38 : 1;
    if (st === 'error' && !reduced) dim *= 0.75 + 0.25 * Math.abs(Math.sin(now / 70));
    var breathe = st === 'idle' && !reduced ? 0.5 + 0.5 * Math.sin(now / 1000 * 1.2) : 0.5;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, w, w);
    g.globalCompositeOperation = 'lighter';

    // haze
    var haze = g.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.65);
    haze.addColorStop(0, rgba(col, (0.10 + 0.05 * breathe + 0.12 * level) * dim));
    haze.addColorStop(1, rgba(col, 0));
    g.fillStyle = haze; g.fillRect(0, 0, w, w);

    // outer tick ring
    g.save(); g.translate(cx, cy); g.rotate(spin * 0.35);
    g.strokeStyle = rgba(col, 0.42 * dim); g.lineWidth = Math.max(1, w / 600);
    g.beginPath();
    for (var i = 0; i < 120; i++) {
      var a = i / 120 * Math.PI * 2, r1 = R * 1.36, r2 = R * (i % 10 === 0 ? 1.43 : 1.39);
      g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1); g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
    }
    g.stroke(); g.restore();

    // counter-rotating arc segments
    g.lineCap = 'round';
    [[1.24, 1, 0.9, 3], [1.29, -0.6, 0.5, 2], [0.78, -1.4, 0.35, 4]].forEach(function (s) {
      g.save(); g.translate(cx, cy); g.rotate(spin * s[1]);
      g.strokeStyle = rgba(col, s[2] * 0.55 * dim); g.lineWidth = Math.max(1, w / 360);
      for (var n = 0; n < s[3]; n++) {
        var a0 = n / s[3] * Math.PI * 2;
        g.beginPath(); g.arc(0, 0, R * s[0], a0, a0 + Math.PI * 2 / s[3] * 0.62); g.stroke();
      }
      g.restore();
    });

    // audio-reactive spikes around the ring
    var spec = spectrum(an), bars = 96;
    g.save(); g.translate(cx, cy); g.rotate(-Math.PI / 2);
    g.strokeStyle = rgba(col, 0.75 * dim); g.lineWidth = Math.max(1.5, w / 260);
    g.beginPath();
    for (var b = 0; b < bars; b++) {
      var m = b < bars / 2 ? b : bars - b;              // mirror so it is symmetric
      var v = spec ? spec[2 + m * 3] / 255 : noise(m, t * 2) * level;
      var len = R * (0.015 + 0.26 * v * (0.35 + level));
      var ang = b / bars * Math.PI * 2, r0 = R * 1.1;
      g.moveTo(Math.cos(ang) * r0, Math.sin(ang) * r0);
      g.lineTo(Math.cos(ang) * (r0 + len), Math.sin(ang) * (r0 + len));
    }
    g.stroke(); g.restore();

    // the main ring: soft torus + bright filament + eroded edge
    var ringR = R * (1 + 0.04 * level + 0.012 * breathe);
    [[0.26, 0.07], [0.12, 0.2], [0.035, 0.7], [0.012, 1]].forEach(function (L) {
      g.beginPath(); g.arc(cx, cy, ringR, 0, Math.PI * 2);
      g.strokeStyle = rgba(col, L[1] * dim * (0.85 + 0.3 * level)); g.lineWidth = R * L[0]; g.stroke();
    });
    g.fillStyle = rgba(col, 0.55 * dim);
    for (var p = 0; p < 220; p++) {
      var pa = p / 220 * Math.PI * 2 + spin * 0.2, nz = noise(p, t);
      var pr = ringR + R * (0.07 + 0.06 * nz + 0.08 * level * nz);
      var ps = Math.max(1, w / 500) * (0.6 + nz);
      g.globalAlpha = 0.25 + 0.6 * nz;
      g.fillRect(cx + Math.cos(pa) * pr, cy + Math.sin(pa) * pr, ps, ps);
    }
    g.globalAlpha = 1;

    // inner disc: fine concentric lines
    g.lineWidth = Math.max(0.5, w / 1200);
    for (var c = 0; c < 9; c++) {
      g.beginPath(); g.arc(cx, cy, R * (0.3 + c * 0.065), 0, Math.PI * 2);
      g.strokeStyle = rgba(col, 0.07 * dim); g.stroke();
    }

    // thinking: orbiting nodes with trails
    if (st === 'thinking') {
      for (var o = 0; o < 3; o++) {
        for (var tr = 0; tr < 10; tr++) {
          var oa = spin * (1.6 + o * 0.5) + o * 2.1 - tr * 0.06, orr = R * (0.62 + o * 0.17);
          g.fillStyle = rgba(col, (1 - tr / 10) * 0.8);
          g.beginPath(); g.arc(cx + Math.cos(oa) * orr, cy + Math.sin(oa) * orr, R * 0.022 * (1 - tr / 14), 0, Math.PI * 2); g.fill();
        }
      }
    }

    // the core
    var coreR = R * (0.26 + 0.16 * level + 0.03 * breathe);
    var core = g.createRadialGradient(cx, cy, 0, cx, cy, coreR * 1.9);
    core.addColorStop(0, rgba([255, 255, 255], 0.9 * dim));
    core.addColorStop(0.22, rgba(col, 0.75 * dim));
    core.addColorStop(0.55, rgba(col, 0.18 * dim));
    core.addColorStop(1, rgba(col, 0));
    g.fillStyle = core; g.beginPath(); g.arc(cx, cy, coreR * 1.9, 0, Math.PI * 2); g.fill();
    g.globalCompositeOperation = 'source-over';

    schedule();
  }
  var slow = null;
  function schedule() {
    if (document.hidden || raf) return;
    if (reduced) { clearTimeout(slow); slow = setTimeout(function () { raf = requestAnimationFrame(frame); }, 125); }
    else raf = requestAnimationFrame(frame);
  }
  fit();
  window.addEventListener('resize', function () { fit(); });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) { pollStatus(); schedule(); } });
  render();
  schedule();
})();
