/* Jarvis view: the voice-first home stage (templates/home.html), live over server-sent events.
   - One warm brain on the server (assistant/brain.py). This page streams everything it says and does from
     GET /voice/api/stream: the turn, its text, each finished sentence, tool steps ("Reading home.py"),
     background tasks, panels from the `display` tool, and voice-setting changes. No polling for answers.
   - Speaking (adapted from adewaskar/jarvis src/lib/tts.ts, MIT License, Copyright (c) 2026 Aditya Dewaskar):
     the browser's own speechSynthesis by default (instant; the best British male voice installed, e.g.
     "Microsoft Ryan Online (Natural)" on Windows, "Google UK English Male" in Chrome), sentence by sentence as
     each one completes, one sentence primed ahead; Kokoro or ElevenLabs through POST /voice/api/tts when chosen.
     Chrome's 15-second cut-off and its wedged-after-cancel bug are handled (pause/resume keepalive, resume
     before every utterance, a start watchdog).
   - Listening (adapted from src/lib/vad.ts and voice.ts): an energy detector with an adaptive noise floor
     finds each spoken segment; the clip goes to POST /voice/api/talk (speech-to-text on the server); an
     assembler holds a sentence that trails off ("remind me to...") for a moment; our own voice coming back
     through the speakers is recognised as echo; fillers ("maybe, um") are ignored. Barge-in: speech while he
     talks pauses him at once, and once the words arrive it either stops him (and the answer in flight) or,
     if it was only noise, lets him carry on.
   - Fillers (src/lib/fillers.ts): one short line the moment the first tool starts, then quiet until the answer.
   - The activity rail: current steps, background tasks with elapsed time, finished tasks with a one-line
     result, and panels. Panel markup is model-written, so it is rebuilt node by node from a strict allowlist
     (sanitizePanel: the same tags and classes as assistant/panels.py; no scripts, handlers, links or styles
     other than --v; images only from http(s) or data:image).
   - Hearing (VOICE_STT auto|browser|server; capabilities as in src/lib/capabilities.ts): in Chrome/Edge the
     browser's own SpeechRecognition transcribes as he speaks and the text goes straight to /voice/api/ask (no
     upload, no Whisper). A heartbeat restarts it when Chrome throttles it into silence (src/lib/voice.ts
     startBrowserVoice); interim results drive barge-in; a "network"/"service" error falls back to the server path.
   - Panels by kind (assistant/hud.py): markup, image, video, YouTube (youtube-nocookie iframe), map (an
     OpenStreetMap tile mosaic, no map library), data, list. Remote images only through /voice/api/img. Any panel
     expands to a large "blade" over the ring (src/ui/Blades.tsx, simplified). The ui_* tools restyle the page
     (accent, ring style/colour/scale/spin, rails, one-off effects) through `ui` events.
   - D toggles diagnostics (mic permission and level, speech engine and last transcript, brain link, last turn
     timings, voice and last spoken); T runs a one-line audio self-test (src/ui/Diagnostics.tsx). The time from the
     end of his speech to the first spoken word is measured here and posted to /voice/api/timing.
   Every user/model string is inserted with textContent or through sanitizePanel. The page itself makes no
   external requests except the YouTube embed frame (CSP frame-src youtube-nocookie.com only). */
(function () {
  'use strict';
  var root = document.querySelector('[data-jarvis]');
  if (!root) return;
  document.documentElement.classList.add('jv-on');
  function q(s) { return root.querySelector(s); }
  var canvas = q('[data-jv-core]'), ptt = q('[data-jv-ptt]'), statusEl = q('[data-jv-status]'),
    youEl = q('[data-jv-you]'), themEl = q('[data-jv-them]'), sysEl = q('[data-jv-sys]'),
    pttLabel = q('[data-jv-ptt-label]'), histBtn = q('[data-jv-history]'), menu = q('[data-jv-menu]'),
    tip = q('[data-jv-tip]'), tipX = q('[data-jv-tip-x]');
  var stepsEl = q('[data-jv-steps]'), tasksEl = q('[data-jv-tasks]'), panelsEl = q('[data-jv-panels]'), liveEl = q('[data-jv-live]');
  var drawer = document.querySelector('[data-jv-drawer]'), scrim = document.querySelector('[data-jv-scrim]');
  var logEl = document.querySelector('[data-chat-log]');
  var csrf = root.getAttribute('data-csrf'), H = root.getAttribute('data-honorific') || 'sir';
  var NAME = root.getAttribute('data-name') || 'Sterling';
  var canTalk = root.getAttribute('data-can-talk') === '1';
  var reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var serverStatus = statusEl.textContent;
  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  /* ---- state ---------------------------------------------------------------------------- */
  var local = null;          // listening | thinking | speaking | error: this tab is busy
  var bg = { state: 'offline', online: false, muted: false };
  var always = false;        // the ring is on: this tab listens continuously
  var errText = '', errTimer = null, note = '', noteTimer = null, working = '', brainState = '';
  var BG = { idle: 'idle', followup: 'listening', listening: 'listening', thinking: 'thinking', speaking: 'speaking',
    muted: 'idle', no_mic: 'idle', starting: 'idle', offline: 'idle', stopped: 'idle' };
  function laptopLive() {
    return !!(bg.online && !bg.muted && bg.state !== 'no_mic' && bg.state !== 'stopped' && bg.state !== 'starting');
  }
  function isOn() { return always || laptopLive(); }
  function visual() {
    if (local) return local;
    if (speaker.busy()) return 'speaking';
    if (pending) return 'thinking';
    if (always) return 'listening';
    if (laptopLive()) return BG[bg.state] || 'idle';
    return 'idle';
  }
  function render() {
    var v = visual(), on = canTalk && isOn(), text;
    root.setAttribute('data-state', v);
    root.setAttribute('data-on', on ? '1' : '0');
    if (!canTalk) text = serverStatus;
    else if (v === 'error') text = errText;
    else if (brainState === 'reconnecting') text = 'Reconnecting…';
    else if (v === 'thinking') text = working || 'Thinking…';
    else if (v === 'speaking') text = 'Speaking — tap to interrupt';
    else if (local === 'listening' || (v === 'listening' && !always && bg.state === 'listening')) text = 'Listening…';
    else if (note) text = note;
    else if (always) text = 'Listening — tap to stop';
    else if (on) text = 'Listening on your laptop mic — tap to stop';
    else text = 'Tap to talk';
    if (statusEl.textContent !== text) statusEl.textContent = text;
    statusEl.classList.toggle('is-long', text.length > 34);
    ptt.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (pttLabel) pttLabel.textContent = v === 'speaking' ? 'Interrupt ' + NAME
      : on ? 'Stop listening' : 'Talk to ' + NAME + ': tap to start listening';
    ptt.classList.toggle('is-live', local === 'listening');
    if (sysEl && canTalk) sysEl.textContent = 'online';
  }
  function fail(msg, ms) {
    errText = msg; local = 'error'; render();
    clearTimeout(errTimer);
    errTimer = setTimeout(function () { if (local === 'error') { local = null; render(); } }, ms || 7000);
  }
  function flash(msg) {
    note = msg; render(); clearTimeout(noteTimer);
    noteTimer = setTimeout(function () { note = ''; render(); }, 3500);
  }
  function post(url, fields) {
    var fd = new FormData(); fd.append('csrf', csrf);
    Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
    return fetch(url, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } });
  }
  function getJSON(r) { return r.json().catch(function () { return {}; }); }

  /* ---- audio graph ------------------------------------------------------------------------ */
  var ctx = null, outAnalyser = null, micAnalyser = null, stream = null, micReady = null, releaseTimer = null, src = null;
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
          micAnalyser = c.createAnalyser(); micAnalyser.fftSize = 1024; micAnalyser.smoothingTimeConstant = 0.35;
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
  function releaseSoon() {
    clearTimeout(releaseTimer);
    releaseTimer = setTimeout(function () { if (!always && !rec) releaseMic(); }, 15000);
  }
  function micError(e) {
    var n = e && e.name;
    always = false;
    if (n === 'NotAllowedError' || n === 'SecurityError') fail('Mic blocked — click the icon left of the address bar, allow Microphone, then tap the ring.', 15000);
    else if (n === 'NotFoundError' || n === 'OverconstrainedError') fail('Mic blocked — no microphone found. Plug one in, then tap the ring.', 15000);
    else if (n === 'NotReadableError') fail('Mic blocked — another app is using it. Close that app, then tap the ring.', 15000);
    else if (n === 'Insecure') fail('Mic blocked — this address is not secure. Open the dashboard at http://127.0.0.1:8000 instead.', 15000);
    else fail('Mic blocked — the browser refused it. Reload the page, then tap the ring.', 15000);
  }
  function rms(an) {
    if (!an) return 0;
    an.getFloatTimeDomainData(tbuf);
    var s = 0;
    for (var i = 0; i < tbuf.length; i++) s += tbuf[i] * tbuf[i];
    return Math.sqrt(s / tbuf.length);
  }

  /* ---- the voice: speechSynthesis by default, Kokoro / ElevenLabs through the server ------- */
  var voiceCfg = { web_tts: 'browser', web_voice: '' };
  var synth = window.speechSynthesis || null;
  var cachedVoice;
  function score(v) {         // a British male, level and natural; Windows' neural voices first
    var n = v.name.toLowerCase(), s = 0;
    if (/ryan.*(online|natural)/.test(n)) s += 130;
    else if (/thomas.*(online|natural)/.test(n)) s += 115;
    else if (/(alfie|elliot|oliver|noah|ethan).*(online|natural)/.test(n)) s += 105;
    else if (/microsoft george/.test(n)) s += 95;
    else if (n.indexOf('google uk english male') >= 0) s += 90;
    else if (/^daniel/.test(n)) s += 88;
    else if (/\b(ryan|thomas|oliver|arthur|jamie|malcolm|george|hazel)\b/.test(n)) s += 60;
    if (/natural|online|neural|premium|enhanced/.test(n)) s += 20;
    if (/en[-_]gb/i.test(v.lang)) s += 25; else if (/^en/i.test(v.lang)) s += 5;
    if (/\b(sonia|libby|maisie|mia|bella|hollie|abbie|olivia|susan|hazel|kate|serena|fiona|moira|karen|tessa|samantha|zira|aria|jenny|ava|emma|female)\b/.test(n)) s -= 70;
    return s;
  }
  function voices() { return synth ? synth.getVoices().filter(function (v) { return /^en/i.test(v.lang); }) : []; }
  function pickVoice() {
    if (cachedVoice !== undefined) return cachedVoice;
    var all = voices();
    if (!all.length) return null;
    var want = (voiceCfg.web_voice || '').toLowerCase(), hit = null;
    if (want) all.forEach(function (v) { if (!hit && v.name.toLowerCase() === want) hit = v; });
    if (!hit && want) all.forEach(function (v) { if (!hit && v.name.toLowerCase().indexOf(want) >= 0) hit = v; });
    if (!hit) hit = all.map(function (v) { return { v: v, s: score(v) }; }).sort(function (a, b) { return b.s - a.s; })[0].v;
    cachedVoice = hit;
    return hit;
  }
  var reported = false;
  function reportVoices() {         // so "switch to Ryan" can find a browser voice by name
    var names = voices().map(function (v) { return v.name; });
    if (reported || !names.length || !canTalk) return;
    reported = true;
    post('/voice/api/voices', { voices: JSON.stringify(names.slice(0, 100)) }).catch(function () { reported = false; });
  }
  if (synth) synth.addEventListener('voiceschanged', function () { cachedVoice = undefined; pickVoice(); reportVoices(); });

  function shape(text) {
    return text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/https?:\/\/[^\s]*[^\s.,;:!?)\]]/g, '')
      .replace(/[*_`#>]+/g, '').replace(/([^,\s])\s+(sir)(\s*[.,!?;:]|\s*$)/gi, '$1, $2$3').replace(/\s+/g, ' ').trim();
  }
  var speaker = (function () {
    var queue = [], current = null, pumping = false, paused = false, speaking = '', speakingAt = 0;
    var recent = '', recentUntil = 0, keepalive = null;
    function setSpeaking(t) {
      if (t) { speaking = t; speakingAt = Date.now(); return; }
      if (speaking) { recent = speaking; recentUntil = Date.now() + 1800; }
      speaking = '';
    }
    function serverAudio(text) {      // Kokoro / ElevenLabs: one sentence primed ahead
      if (voiceCfg.web_tts === 'browser') return null;
      var c = audioCtx();
      if (!c) return null;
      return post('/voice/api/tts', { text: text }).then(function (r) { if (!r.ok) throw new Error('tts'); return r.arrayBuffer(); })
        .then(function (ab) { return new Promise(function (res, rej) { c.decodeAudioData(ab, res, rej); }); })
        .catch(function () { return null; });
    }
    function prime(item) { if (item && item.audio === undefined) item.audio = serverAudio(item.text); }
    function begun(item) {               // the first sound of an item: diagnostics, self-test, turn timing
      diag.ttsStarted++; diag.spoken = item.text; diag.spokenAt = Date.now();
      if (item.onstart) { var f = item.onstart; item.onstart = null; try { f(); } catch (e) { /* ignore */ } }
      firstWord(item.turn);
    }
    function playBuffer(buffer, item) {
      return new Promise(function (resolve) {
        var c = audioCtx(), s = c.createBufferSource();
        s.buffer = buffer; s.connect(outAnalyser);
        var done = false;
        function fin() { if (done) return; done = true; if (src === s) src = null; resolve(); }
        s.onended = fin; s.__stop = fin;
        src = s; s.start(); begun(item);
      });
    }
    function speakNative(text, item) {
      return new Promise(function (resolve) {
        if (!synth) { resolve(false); return; }
        synth.resume();               // Chrome wedges after cancel(); resume() un-wedges it
        var u = new SpeechSynthesisUtterance(text), v = pickVoice();
        if (v) u.voice = v;
        u.lang = (v && v.lang) || 'en-GB'; u.rate = 1.0; u.pitch = 0.95;
        var started = false, done = false, watchdog = null;
        function fin() {
          if (done) return; done = true; clearTimeout(watchdog); clearInterval(keepalive); keepalive = null;
          if (!started) diag.ttsFailures++;
          resolve(started);
        }
        u.onstart = function () {
          started = true; clearTimeout(watchdog); begun(item);
          keepalive = setInterval(function () { if (!paused && synth.speaking) { synth.pause(); synth.resume(); } }, 5000);
        };
        u.onend = fin; u.onerror = fin;
        watchdog = setTimeout(function () {
          if (started || done) return;
          synth.cancel(); synth.resume();
          try { synth.speak(u); } catch (e) { fin(); return; }
          watchdog = setTimeout(function () { if (!started) fin(); }, 1500);
        }, 900);
        current = { native: true };
        synth.speak(u);
      });
    }
    function pump() {
      if (pumping) return;
      pumping = true;
      (function next() {
        var item = queue.shift();
        if (!item) { pumping = false; current = null; setSpeaking(''); render(); onIdle(); return; }
        prime(item); prime(queue[0]);
        setSpeaking(item.text); render();
        Promise.resolve(item.audio).then(function (buffer) {
          if (item.dead) return null;
          if (buffer) { current = { buffer: true }; diag.tts = voiceCfg.web_tts + ' (server voice)'; return playBuffer(buffer, item); }
          var nv = pickVoice(); diag.tts = 'browser: ' + (nv ? nv.name : 'default voice');
          return speakNative(item.text, item);
        }).then(function () { setSpeaking(''); next(); }, function () { setSpeaking(''); next(); });
      })();
    }
    return {
      say: function (text, turn, front, onstart) {
        var t = shape(text);
        if (!t) return;
        var item = { text: t, turn: turn, onstart: onstart || null };
        if (front) queue.unshift(item); else queue.push(item);
        pump();
      },
      busy: function () { return pumping || !!speaking; },
      since: function () { return speaking ? speakingAt : 0; },
      now: function () { return (speaking + ' ' + (Date.now() < recentUntil ? recent : '')).trim(); },
      pause: function () {
        if (!this.busy() || paused) return false;
        paused = true;
        if (synth && synth.speaking) synth.pause();
        if (ctx && src) ctx.suspend();
        return true;
      },
      resume: function () {
        if (!paused) return;
        paused = false;
        if (synth) synth.resume();
        if (ctx && ctx.state === 'suspended') ctx.resume();
      },
      cancel: function () {
        queue.forEach(function (i) { i.dead = true; }); queue = [];
        paused = false;
        if (synth) { synth.cancel(); synth.resume(); }
        if (src) { var s = src; src = null; try { s.stop(); } catch (e) { /* stopped */ } if (s.__stop) s.__stop(); }
        if (ctx && ctx.state === 'suspended') ctx.resume();
        setSpeaking('');
      },
      isPaused: function () { return paused; }
    };
  })();
  function onIdle() { /* nothing queued: the ring settles; his reply window opens */
    firstInWindow = true;
    if (!pending && local === 'speaking') local = null; render();
  }

  /* ---- fillers (adewaskar/jarvis src/lib/fillers.ts: subjectless, short, "sir" final and only sometimes) */
  function picker(pool) { var last = -1; return function () { var i = last; while (pool.length > 1 && i === last) i = Math.floor(Math.random() * pool.length); last = i; return pool[Math.max(0, i)].replace('{h}', H); }; }
  var WORKING = picker(['Working on it, {h}.', 'Under way.', 'Retrieving.', 'Cross-referencing.', 'Checking.']);
  var BY_TOOL = [
    [/test/i, picker(['Running the tests.', 'Testing it now.'])],
    [/^(reading|editing|writing|searching the code)/i, picker(['Looking through the code.', 'Opening the files.'])],
    [/web/i, picker(['Searching.', 'Consulting the record.'])],
    [/^opening/i, picker(['Opening it now.', 'Bringing it up.'])],
    [/notes|journal|noting/i, picker(['Checking my notes.', 'Consulting the archive.'])],
    [/voice|setting/i, picker(['Very good, {h}.', 'Directly, {h}.'])],
    [/shorts|books|hours|goals|usage/i, picker(['Pulling the figures.', 'Checking the numbers.'])],
    [/map|where/i, picker(['Plotting it.', 'Bringing up the map.'])],
    [/screen|picture|video|showing/i, picker(['Putting it up.', 'On screen shortly.'])],
    [/background task|helper/i, picker(['Setting it going.', 'Under way.'])]
  ];
  function fillerFor(label) {
    for (var i = 0; i < BY_TOOL.length; i++) if (BY_TOOL[i][0].test(label)) return BY_TOOL[i][1]();
    return WORKING();
  }

  /* ---- what he says: segments -> words -> one utterance (src/lib/voice.ts makeAssembler) ---- */
  var CONTINUES = /\b(and|or|but|so|because|since|if|when|while|that|which|who|to|of|in|on|at|by|for|with|from|about|into|the|a|an|my|your|his|her|its|our|their|is|are|was|were|be|do|does|did|have|has|had|can|could|would|should|will|might|must|like|than|then|as|very|really|just|some|any)$/i;
  var OVERRIDE = /\b(stop|wait|cancel|enough|quiet|hold on|shut up|never ?mind|forget it|no)\b/i;
  var STOPW = ('a an the and or but so of to in on at by for with from is are was were be it its this that these those i you he she we they me him her them my your our their what which who how why when where do does did can could would should will shall not no yes if then than as about into over under out up down one two three first second third now here there just very really got get have has had say said tell okay ok well right sir').split(' ');
  function norm(s) { return s.toLowerCase().replace(/[^a-z0-9' ]+/g, ' ').replace(/\s+/g, ' ').trim(); }
  function isEcho(heard, spoken) {         // is the mic hearing the speakers? (bags of distinctive words)
    if (!spoken || OVERRIDE.test(heard)) return false;
    var all = norm(heard).split(' ').filter(Boolean);
    if (!all.length) return true;
    var mine = {}; norm(spoken).split(' ').forEach(function (w) { mine[w] = 1; });
    var content = all.filter(function (w) { return STOPW.indexOf(w) < 0; });
    if (content.length < 2) return all.length >= 2 && all.every(function (w) { return mine[w]; });
    var hits = content.filter(function (w) { return mine[w]; }).length;
    return hits / content.length >= 0.6;
  }
  // A clause that dangles even though the transcriber put a full stop after it ("On Wednesday.", "...and.")
  var LEADS = /^(on|and|so|but|then|also|plus|or|for|at|in|by|with|after|before|until|from)$/i;
  function holdFor(text) {
    var t = text.trim(), words = t.split(/\s+/).filter(Boolean);
    if (!words.length) return 1600;
    var bare = t.replace(/[.!?…]+$/, '').trim();
    if (/[,;:–—-]$/.test(bare) || /(\.\.\.|…)$/.test(t)) return 1600;
    var last = (bare.split(/\s+/).pop() || '').replace(/[^a-z']/gi, '');
    if (CONTINUES.test(last)) return 1600;
    var tail = bare.split(/[.!?]\s+/).pop().trim().split(/\s+/), lead = (tail[0] || '').replace(/[^a-z']/gi, '');
    if (tail.length <= 3 && LEADS.test(lead) && !OVERRIDE.test(bare)) return 1600;
    if (/[.!?]$/.test(t)) return 0;
    return 250;
  }
  var held = '', holdTimer = null, firstAt = 0;
  function assemble(text, active) {
    held = (held + ' ' + text).replace(/\s+/g, ' ').trim();
    if (!firstAt) firstAt = Date.now();
    youEl.textContent = held;
    clearTimeout(holdTimer);
    if (active) { holdTimer = setTimeout(fire, 6000); return; }
    var wait = Math.min(holdFor(held), Math.max(0, 6000 - (Date.now() - firstAt)));
    if (!wait) fire(); else holdTimer = setTimeout(fire, wait);
  }
  function fire() {
    clearTimeout(holdTimer);
    var t = held.trim(); held = ''; firstAt = 0;
    if (t) ask(t);
  }

  /* ---- asking the brain ------------------------------------------------------------------- */
  var myTurns = {}, cancelled = {}, pending = null, turnKinds = {}, spokeIn = {}, filled = {};
  var timing = {}, speechEndAt = 0, heardBy = '', heardSttMs = 0;
  function firstWord(turn) {            // end of his speech -> first spoken word, measured here
    var t = turn && timing[turn];
    if (!t || t.sent) return;
    t.sent = true;
    var ms = Date.now() - t.end;
    diag.lastTurn = { ms: ms, how: t.how, ask: t.askMs, stt: t.sttMs, at: Date.now() };
    post('/voice/api/timing', { turn: turn, first_word_ms: ms, stt: t.how, ask_ms: t.askMs, stt_ms: t.sttMs || '',
      tts: voiceCfg.web_tts || 'browser' }).catch(function () { /* diagnostics only */ });
  }
  function ask(text) {
    youEl.textContent = text;
    pending = 'asking'; render();
    var askAt = Date.now(), t0 = { end: speechEndAt || askAt, how: heardBy || 'typed', sttMs: heardSttMs };
    speechEndAt = 0; heardBy = ''; heardSttMs = 0;
    post('/voice/api/ask', { text: text, origin: 'browser' }).then(getJSON).then(function (j) {
      if (j.ok && j.turn) {
        t0.askMs = Date.now() - askAt; timing[j.turn] = t0;
        if (j.merged) { cancelled[j.merged] = 1; youEl.textContent = j.heard || text; }   // stitched onto the last breath
        myTurns[j.turn] = 1; pending = j.turn; addHistory('me', text, 'voice · this tab');
      }
      else {
        pending = null;
        if (j.quiet) youEl.textContent = '';              // a filler or noise: keep listening, say nothing
        else if (j.error) fail(j.msg || j.reason || 'That did not go through.');
      }
      render();
    }).catch(function () { pending = null; fail('The connection dropped. Try again.'); });
  }
  function interrupt() {
    if (pending && pending !== 'asking') { cancelled[pending] = 1; pending = null; }
    speaker.cancel();
    post('/voice/api/interrupt', {}).catch(function () { /* nothing in flight */ });
    working = ''; render();
  }

  /* ---- recording segments ----------------------------------------------------------------- */
  var rec = null, recMode = '', recStart = 0, maxTimer = null, pttDown = false, discard = false;
  function mimeType() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
    var types = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    for (var i = 0; i < types.length; i++) if (MediaRecorder.isTypeSupported(types[i])) return types[i];
    return '';
  }
  function startRec(mode) {
    if (!canTalk || rec) return;
    audioCtx();
    ensureMic().then(function (s) {
      if (rec || (mode === 'ptt' && !pttDown)) return;
      if (!window.MediaRecorder) { fail('This browser cannot record audio. Type in History instead.'); return; }
      var type = mimeType(), chunks = [];
      try { rec = type ? new MediaRecorder(s, { mimeType: type }) : new MediaRecorder(s); } catch (e) { fail('This browser cannot record audio.'); return; }
      recMode = mode; recStart = performance.now(); discard = false;
      rec.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
      rec.onstop = function () {
        var r = rec, dur = performance.now() - recStart, m = recMode; rec = null;
        var blob = new Blob(chunks, { type: (r && r.mimeType) || type || 'audio/webm' });
        if (!always) releaseSoon();
        if (local === 'listening') local = null;
        if (discard || dur < 300 || blob.size < 600) { speaker.resume(); render(); return; }
        transcribe(blob, m);
      };
      rec.start(250);
      if (mode === 'ptt') { speaker.cancel(); interrupt(); }
      local = 'listening'; render();
      clearTimeout(maxTimer); maxTimer = setTimeout(function () { stopRec(); }, 20000);
    }, micError);
  }
  function stopRec(drop) {
    clearTimeout(maxTimer);
    if (rec && rec.state !== 'inactive') { discard = !!drop; rec.stop(); }
  }
  function transcribe(blob, mode) {
    var fd = new FormData();
    fd.append('csrf', csrf);
    fd.append('audio', blob, 'speech.' + (/mp4/.test(blob.type) ? 'm4a' : /ogg/.test(blob.type) ? 'ogg' : 'webm'));
    fetch('/voice/api/talk', { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(getJSON).then(function (j) {
        if (!j.ok) {
          speaker.resume();
          if (!j.quiet && j.error !== 'no_speech') fail(j.msg || j.reason || 'That did not go through.');
          render(); return;
        }
        if (j.command) { speaker.cancel(); interrupt(); command(j.command); return; }
        var heard = j.heard || '';
        diag.heard = heard; diag.heardAt = Date.now(); heardSttMs = j.stt_ms || 0;
        if (speaker.busy() && isEcho(heard, speaker.now())) { speaker.resume(); return; }   // his own voice
        // Only talking over his answer is a barge-in. While he has not answered yet (at most a filler), the server
        // stitches this onto the last utterance: one request, not a cancelled one and a new one.
        if (speaker.busy() && pending && pending !== 'asking' && !spokeIn[pending]) speaker.cancel();
        else if (speaker.busy()) interrupt();
        if (mode === 'ptt') { held = ''; ask(heard); } else assemble(heard, !!rec);
      }).catch(function () { speaker.resume(); fail('The connection dropped. Try again.'); });
  }
  function command(cmd) {
    if (cmd === 'mute') { setOn(false); caption('Microphone off, ' + H + '. Tap the ring when you want me.'); }
    else { local = null; caption('Very good, ' + H + '.'); render(); }
  }

  /* ---- hearing in the browser: SpeechRecognition with a heartbeat ---------------------------------
     Adapted from adewaskar/jarvis src/lib/voice.ts startBrowserVoice and capabilities.ts (MIT License). Chrome
     and Edge transcribe as he speaks; the text goes straight to /voice/api/ask. Chrome throttles the engine and
     it can go silent with no event to catch, so a heartbeat tears it down and builds a fresh one. A "network"
     or "service-not-allowed" error (no speech service reachable) falls back to Whisper on the server. */
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var sttBroken = '';
  var diag = { sttRunning: false, sessions: 0, restarts: 0, heard: '', heardAt: 0, interim: '', sttError: '', idleMs: 0,
    tts: '', spoken: '', spokenAt: 0, ttsStarted: 0, ttsFailures: 0, lastTurn: null, serverTiming: null,
    micPerm: 'unknown', selfTest: '' };
  function sttEngine() {
    var want = voiceCfg.stt || 'auto';
    if (want === 'server' || !SR || sttBroken) return 'server';
    return 'browser';
  }
  function sttLabel() {
    var e = sttEngine();
    return e === 'browser' ? 'browser speech recognition' : 'Whisper on the server' +
      (sttBroken ? ' (browser engine failed: ' + sttBroken + ')' : !SR && (voiceCfg.stt || 'auto') !== 'server' ? ' (this browser has none)' : '');
  }
  function localCommand(t) {      // the same local commands as voice/commands.py, no Claude involved
    var n = norm(t).replace(new RegExp('^(?:hey |ok |okay )?(?:' + NAME.toLowerCase() + '|jarvis)\\s*'), '').trim();
    if (/^(stop listening|mute|mute yourself|go to sleep)$/.test(n)) return 'mute';
    if (/^(stop|quiet|be quiet|shush|stop talking|enough|that's enough)$/.test(n)) return 'stop';
    if (/^(never ?mind|cancel|forget it|cancel that)$/.test(n)) return 'cancel';
    return null;
  }
  function heardText(heard, endAt, how, mode) {
    if (local === 'listening') local = null;
    var cmd = localCommand(heard);
    if (cmd) { speaker.cancel(); interrupt(); command(cmd); return; }
    if (speaker.busy() && isEcho(heard, speaker.now())) { speaker.resume(); render(); return; }   // his own voice
    if (speaker.busy() && pending && pending !== 'asking' && !spokeIn[pending]) speaker.cancel();
    else if (speaker.busy()) interrupt();
    speechEndAt = endAt || Date.now(); heardBy = how; heardSttMs = 0;
    if (mode === 'ptt') { held = ''; ask(heard); } else assemble(heard, false);
    render();
  }
  var bsr = (function () {
    var rec = null, running = false, wanted = false, settled = '', interim = '', lastAlive = 0, lastResultAt = 0;
    var silence = null, started = false, health = null;
    function touch() { lastAlive = Date.now(); }
    function reset() { clearTimeout(silence); settled = ''; interim = ''; started = false; }
    function emit() {
      var text = (settled + ' ' + interim).replace(/\s+/g, ' ').trim(), endAt = lastResultAt;
      reset();
      if (!text) { if (local === 'listening') { local = null; render(); } return; }
      diag.heard = text; diag.heardAt = Date.now();
      heardText(text, endAt, 'browser', pttDown || pttBrowser ? 'ptt' : 'vad');
    }
    function onResult(e) {
      touch(); lastResultAt = Date.now();
      var fresh = ''; interim = '';
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var chunk = e.results[i][0].transcript;
        if (e.results[i].isFinal) fresh += chunk; else interim += chunk;
      }
      settled = (settled + ' ' + fresh).replace(/\s+/g, ' ').trim();
      var full = (settled + ' ' + interim).replace(/\s+/g, ' ').trim();
      diag.interim = full;
      if (!full) return;
      if (speaker.busy() && isEcho(full, speaker.now())) { settled = ''; interim = ''; return; }
      if (!started) {
        if (speaker.busy() && !OVERRIDE.test(full)) {
          if (Date.now() - speaker.since() < 350) return;          // his own first syllable
          if (full.split(/\s+/).length < 2) return;               // one word of his own playback is no evidence
        }
        started = true;
        if (speaker.busy()) speaker.pause();                         // barge-in on the interim result
        local = 'listening'; render();
      }
      youEl.textContent = (held ? held + ' ' : '') + full;
      clearTimeout(silence);
      silence = setTimeout(emit, pttDown ? 6000 : 900);
    }
    function spin() {
      if (!wanted || running || rec) return;
      try { rec = new SR(); } catch (e) { broken('unavailable'); return; }
      rec.continuous = true; rec.interimResults = true; rec.maxAlternatives = 1;
      rec.lang = /^en/i.test(navigator.language || '') ? navigator.language : 'en-US';
      rec.onstart = function () { running = true; diag.sttRunning = true; diag.sessions++; touch(); };
      rec.onresult = onResult;
      rec.onerror = function (ev) {
        var err = String((ev && ev.error) || '');
        diag.sttError = err; touch();
        if (err === 'not-allowed') { wanted = false; micError({ name: 'NotAllowedError' }); }
        else if (err === 'network' || err === 'service-not-allowed' || err === 'language-not-supported' || err === 'audio-capture') broken(err);
      };
      rec.onend = function () {
        running = false; diag.sttRunning = false; rec = null; touch();
        if (wanted) setTimeout(spin, 80);
      };
      try { rec.start(); } catch (e) { running = false; rec = null; setTimeout(spin, 250); }
    }
    function broken(why) {
      sttBroken = why; diag.sttError = why;
      stop();
      if (always) { flash('Browser speech recognition is unavailable; listening through the laptop instead.'); vad.armed = 0; vad.speaking = false; }
      render();
    }
    function start() {
      if (wanted || !SR) return;
      wanted = true; touch(); spin();
      clearInterval(health);
      health = setInterval(function () {      // the heartbeat
        if (!wanted) return;
        var idle = Date.now() - lastAlive; diag.idleMs = idle;
        if (idle < 15000) return;
        diag.restarts++;
        try { if (rec) rec.abort(); } catch (e) { /* already gone */ }
        rec = null; running = false; diag.sttRunning = false; touch(); spin();
      }, 5000);
    }
    function stop() {
      wanted = false; clearInterval(health); reset();
      try { if (rec) rec.abort(); } catch (e) { /* noop */ }
      rec = null; running = false; diag.sttRunning = false;
    }
    return { start: start, stop: stop, flush: function () { clearTimeout(silence); emit(); }, live: function () { return running; },
      wanted: function () { return wanted; } };
  })();
  var pttBrowser = false;

  /* ---- always-listen: energy VAD with an adaptive floor (src/lib/vad.ts) -------------------- */
  var vad = { floor: 0.01, smooth: 0, armed: 0, speaking: false, lastLoud: 0 }, firstInWindow = true;
  function endSilenceMs() {      // longer for the first thing he says after a reply (he draws breath mid-answer)
    if (speaker.busy()) return 650;
    var long = Math.round(1000 * (parseFloat(voiceCfg.end_silence_sec) || 1.2));
    return firstInWindow || (pending && pending !== 'asking' && !spokeIn[pending]) ? Math.max(650, long) : 650;
  }
  setInterval(function () {
    if (!always || !micAnalyser || recMode === 'ptt' && rec || sttEngine() === 'browser') return;
    var e = rms(micAnalyser), now = performance.now();
    vad.smooth += (e - vad.smooth) * 0.5;
    if (!vad.speaking && !vad.armed) {
      vad.floor += (vad.smooth - vad.floor) * (vad.smooth > vad.floor ? 0.0008 * 6 : 0.02 * 6);
      vad.floor = Math.max(vad.floor, 0.0015);
    }
    var thr = Math.max(0.012, vad.floor * 2.6) * (speaker.busy() ? 2.4 : 1);   // guard: his own voice leaks back
    if (!vad.speaking) {
      if (vad.smooth > thr) {
        if (!vad.armed) { vad.armed = now; startRec('vad'); }
        else if (now - vad.armed >= 110) {
          vad.speaking = true; vad.lastLoud = now;
          // barge-in: not in his own first syllable (350 ms); pause, decide once the words are in
          if (speaker.busy() && Date.now() - speaker.since() > 350) speaker.pause();
        }
      } else if (vad.armed) { vad.armed = 0; stopRec(true); }
    } else {
      if (vad.smooth > thr * 0.6) vad.lastLoud = now;
      if (now - vad.lastLoud >= endSilenceMs() || now - vad.armed >= 20000) {
        speechEndAt = Date.now() - Math.round(now - vad.lastLoud); heardBy = 'server';
        vad.speaking = false; vad.armed = 0; firstInWindow = false; stopRec();
      }
    }
  }, 40);

  /* ---- captions (the reply resolves out of glyph noise, as in adewaskar/jarvis DecodeText) ---- */
  var GLYPHS = '/\\|<>[]{}=+*#%&0123456789ABCDEFGHJKLMNPQRSTUVWXYZ', capTimer = null, capShown = 0;
  function caption(text, keep) {
    clearInterval(capTimer);
    if (reduced || !text) { themEl.textContent = text; capShown = text.length; return; }
    var shown = keep ? Math.min(capShown, text.length) : 0, rate = Math.max(2, Math.ceil(text.length / 40));
    capTimer = setInterval(function () {
      shown = Math.min(text.length, shown + rate);
      var noise = '';
      for (var i = shown; i < Math.min(text.length, shown + 10); i++) noise += text[i] === ' ' ? ' ' : GLYPHS[(i * 7919 + shown * 31) % GLYPHS.length];
      themEl.textContent = text.slice(0, shown) + noise;
      capShown = shown;
      if (shown >= text.length) clearInterval(capTimer);
    }, 33);
  }
  function addHistory(who, text, meta) {
    if (!logEl || !text) return;
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

  /* ---- panels: model-written markup, rebuilt from a strict allowlist (= assistant/panels.py) ---- */
  var P_TAGS = ['div', 'span', 'p', 'ul', 'ol', 'li', 'img', 'b', 'strong', 'em', 'i', 'br', 'small', 'table', 'thead',
    'tbody', 'tr', 'td', 'th', 'code', 'pre', 'h3', 'h4'];
  var P_CLASSES = ['hud-rows', 'hud-row', 'hud-idx', 'hud-main', 'hud-label', 'hud-sub', 'hud-tag', 'hud-metric', 'hud-unit',
    'hud-note', 'hud-img', 'hud-caption', 'hud-grid', 'hud-bar', 'hud-dim', 'hud-hot', 'hud-gallery', 'hud-thumb', 'hud-figure'];
  var P_DROP = ['script', 'style', 'iframe', 'object', 'embed', 'svg', 'math', 'template', 'noscript', 'textarea', 'select',
    'button', 'form', 'frame', 'frameset', 'head', 'title', 'video', 'audio', 'canvas', 'link', 'meta', 'base'];
  var SRC_OK = /^(?:https?:\/\/[^\s"'<>`]+|data:image\/(?:png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=]+)$/i;
  function copyInto(from, to, depth) {
    if (depth > 12) return;
    for (var n = from.firstChild; n; n = n.nextSibling) {
      if (n.nodeType === 3) { to.appendChild(document.createTextNode(n.nodeValue)); continue; }
      if (n.nodeType !== 1) continue;
      var tag = n.tagName.toLowerCase();
      if (P_DROP.indexOf(tag) >= 0) continue;
      if (P_TAGS.indexOf(tag) < 0) { copyInto(n, to, depth + 1); continue; }     // unwrap, keep its text
      var el = document.createElement(tag);
      var cls = (n.getAttribute('class') || '').split(/\s+/).filter(function (c) { return P_CLASSES.indexOf(c) >= 0; });
      if (cls.length) el.className = cls.join(' ');
      var v = /--v\s*:\s*([0-9]*\.?[0-9]+)/.exec(n.getAttribute('style') || '');
      if (v) el.style.setProperty('--v', String(Math.min(1, parseFloat(v[1]))));
      ['alt', 'title'].forEach(function (a) { if (n.hasAttribute(a)) el.setAttribute(a, n.getAttribute(a).slice(0, 200)); });
      ['colspan', 'rowspan'].forEach(function (a) { var x = n.getAttribute(a); if (x && /^\d{1,2}$/.test(x)) el.setAttribute(a, x); });
      if (tag === 'img') {
        var s = (n.getAttribute('src') || '').trim();
        if (!SRC_OK.test(s)) continue;
        el.referrerPolicy = 'no-referrer'; el.loading = 'lazy';
        el.src = /^https?:/i.test(s) ? proxyImg(s) : s;      // remote images only through the guarded proxy
      } else if (tag !== 'br') copyInto(n, el, depth + 1);
      to.appendChild(el);
    }
  }
  function proxyImg(u) { return '/voice/api/img?u=' + encodeURIComponent(u); }
  function sanitizePanel(html) {
    var doc = new DOMParser().parseFromString('<!doctype html><body>' + String(html || ''), 'text/html');
    var frag = document.createDocumentFragment();
    copyInto(doc.body, frag, 0);
    return frag;
  }
  window.__sanitizePanel = function (html) { var d = document.createElement('div'); d.appendChild(sanitizePanel(html)); return d.innerHTML; };

  /* ---- the activity rail ------------------------------------------------------------------- */
  var act = { turns: {}, order: [], tasks: {}, panels: [] };
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function elapsed(iso, until) {
    var t0 = Date.parse(iso || ''), t1 = until ? Date.parse(until) : Date.now();
    if (!t0) return '';
    var s = Math.max(0, Math.round((t1 - t0) / 1000));
    return s < 60 ? s + 's' : s < 3600 ? Math.floor(s / 60) + 'm ' + (s % 60) + 's' : Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm';
  }
  function renderSteps() {
    var id = act.order[act.order.length - 1], t = id && act.turns[id];
    stepsEl.textContent = '';
    if (liveEl) liveEl.hidden = !(t && t.status === 'running');
    if (!t) { stepsEl.appendChild(el('li', 'rail-empty', 'Standing by.')); return; }
    if (t.heard) stepsEl.appendChild(el('li', 'rail-heard', '› ' + t.heard));
    else if (t.kind === 'announce') stepsEl.appendChild(el('li', 'rail-heard', 'Reporting back'));
    t.steps.slice(-12).forEach(function (s) {
      var li = el('li', 'rail-step is-' + s.status + (s.parent ? ' is-sub' : ''));
      li.appendChild(el('span', 'rail-dot')); li.appendChild(el('span', '', s.label));
      stepsEl.appendChild(li);
    });
    var li = el('li', 'rail-step is-' + (t.status === 'running' ? 'running' : (t.status === 'ok' ? 'ok' : 'error')));
    li.appendChild(el('span', 'rail-dot'));
    li.appendChild(el('span', '', t.status === 'running' ? (t.text ? 'Answering' : 'Thinking') : t.status === 'ok' ? 'Answered' :
      t.status === 'cancelled' ? 'Stopped' : 'Did not finish'));
    stepsEl.appendChild(li);
  }
  function renderTasks() {
    var list = Object.keys(act.tasks).map(function (k) { return act.tasks[k]; })
      .sort(function (a, b) { return (a.started || '') < (b.started || '') ? -1 : 1; }).slice(-6).reverse();
    tasksEl.textContent = '';
    if (!list.length) { tasksEl.appendChild(el('li', 'rail-empty', 'Nothing running in the background.')); return; }
    list.forEach(function (t) {
      var li = el('li', 'rail-task is-' + (t.status || 'running'));
      li.appendChild(el('b', '', t.task || 'Task'));
      var steps = t.steps || [], lastStep = steps.length ? steps[steps.length - 1].label : '';
      var line = t.status === 'running' ? 'Working · ' + elapsed(t.started) + (lastStep ? ' · ' + lastStep : '')
        : (t.status === 'done' ? 'Done' : cap(t.status || 'stopped')) + ' · ' + elapsed(t.started, t.finished) + (t.summary ? ' · ' + t.summary : '');
      var small = el('small', '', line); small.title = line;
      li.appendChild(small);
      tasksEl.appendChild(li);
    });
  }
  /* panels by kind (assistant/hud.py); every value is checked again here before it reaches the DOM */
  var TILE = 256, YT_ID = /^[A-Za-z0-9_-]{11}$/, LOCAL_MEDIA = /^\/voice\/api\/(img|media)\?u=|^\/voice\/api\/clip\/\d+$/;
  function lonX(lon, z) { return (lon + 180) / 360 * Math.pow(2, z) * TILE; }
  function latY(lat, z) { var r = lat * Math.PI / 180; return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * Math.pow(2, z) * TILE; }
  function drawMap(box, m, zoom) {      // a tile mosaic centred on the place, with pins (no map library)
    box.textContent = '';
    var W = Math.max(160, box.clientWidth || 300), Hh = Math.max(120, box.clientHeight || 190), z = Math.max(1, Math.min(18, zoom | 0));
    var n = Math.pow(2, z), left = lonX(+m.lon, z) - W / 2, top = latY(+m.lat, z) - Hh / 2;
    for (var ty = Math.floor(top / TILE); ty <= Math.floor((top + Hh) / TILE); ty++) {
      if (ty < 0 || ty >= n) continue;
      for (var tx = Math.floor(left / TILE); tx <= Math.floor((left + W) / TILE); tx++) {
        var img = document.createElement('img'), wx = ((tx % n) + n) % n;
        img.className = 'hud-tile'; img.alt = ''; img.draggable = false; img.referrerPolicy = 'no-referrer';
        img.style.left = Math.round(tx * TILE - left) + 'px'; img.style.top = Math.round(ty * TILE - top) + 'px';
        img.src = proxyImg('https://tile.openstreetmap.org/' + z + '/' + wx + '/' + ty + '.png');
        box.appendChild(img);
      }
    }
    (m.markers || []).slice(0, 8).forEach(function (mk) {
      var x = lonX(+mk.lon, z) - left, y = latY(+mk.lat, z) - top;
      if (!(x >= 0 && x <= W && y >= 0 && y <= Hh)) return;
      var pin = el('span', 'hud-pin'); pin.style.left = Math.round(x) + 'px'; pin.style.top = Math.round(y) + 'px';
      if (mk.label) { pin.title = String(mk.label); pin.appendChild(el('span', 'hud-pin-label', String(mk.label).slice(0, 40))); }
      box.appendChild(pin);
    });
    box.appendChild(el('span', 'hud-attrib', '\u00a9 OpenStreetMap contributors'));
  }
  function mapView(p, big) {
    var m = p.map || {}, wrap = el('div', 'hud-mapwrap'), box = el('div', 'hud-map'), zoom = +m.zoom || 14;
    box.setAttribute('role', 'img'); box.setAttribute('aria-label', 'Map: ' + (m.label || p.title || ''));
    wrap.appendChild(box);
    var ctl = el('div', 'hud-mapctl');
    [['+', 1], ['\u2212', -1]].forEach(function (b) {
      var btn = el('button', 'hud-mapbtn', b[0]); btn.type = 'button'; btn.setAttribute('aria-label', b[1] > 0 ? 'Zoom in' : 'Zoom out');
      btn.addEventListener('click', function (e) { e.stopPropagation(); zoom = Math.max(1, Math.min(18, zoom + b[1])); drawMap(box, m, zoom); });
      ctl.appendChild(btn);
    });
    wrap.appendChild(ctl);
    if (m.label) wrap.appendChild(el('small', 'hud-caption', String(m.label)));
    requestAnimationFrame(function () { drawMap(box, m, zoom); });
    return wrap;
  }
  function panelBody(p, big) {
    var body = el('div', 'hud-body'), k = p.kind || 'markup';
    if (k === 'markup') body.appendChild(sanitizePanel(p.html));
    else if (k === 'map') body.appendChild(mapView(p, big));
    else if (k === 'image' && LOCAL_MEDIA.test(p.src || '')) {
      var im = el('img', 'hud-img'); im.referrerPolicy = 'no-referrer'; im.alt = p.caption || p.title || ''; im.src = p.src;
      body.appendChild(im);
    } else if (k === 'video' && LOCAL_MEDIA.test(p.src || '')) {
      var v = document.createElement('video'); v.className = 'hud-video'; v.controls = true; v.playsInline = true; v.preload = 'metadata'; v.src = p.src;
      body.appendChild(v);
    } else if (k === 'youtube' && YT_ID.test(p.video_id || '')) {
      var f = document.createElement('iframe'); f.className = 'hud-embed';
      f.src = 'https://www.youtube-nocookie.com/embed/' + p.video_id + '?rel=0&modestbranding=1';
      f.title = p.caption || p.title || 'Video'; f.allow = 'accelerometer; encrypted-media; picture-in-picture; fullscreen';
      f.referrerPolicy = 'strict-origin-when-cross-origin'; f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
      f.allowFullscreen = true; f.loading = 'lazy';
      body.appendChild(f);
    } else if (k === 'data') {
      var t = el('table', 'hud-kv');
      (p.rows || []).slice(0, 16).forEach(function (r) { var tr = el('tr'); tr.appendChild(el('th', '', String(r[0] || ''))); tr.appendChild(el('td', '', String(r[1] || ''))); t.appendChild(tr); });
      body.appendChild(t);
    } else if (k === 'list') {
      var rows = el('div', 'hud-rows');
      (p.items || []).slice(0, 12).forEach(function (it, i) {
        var row = el('div', 'hud-row'); row.appendChild(el('span', 'hud-idx', (i < 9 ? '0' : '') + (i + 1)));
        var main = el('span', 'hud-main'); main.appendChild(el('span', 'hud-label', String(it.label || '')));
        if (it.sub) main.appendChild(el('span', 'hud-sub', String(it.sub)));
        row.appendChild(main); if (it.tag) row.appendChild(el('span', 'hud-tag', String(it.tag)));
        rows.appendChild(row);
      });
      body.appendChild(rows);
    } else body.appendChild(el('p', 'hud-note hud-dim', 'Nothing to show.'));
    if (p.caption && k !== 'map') body.appendChild(el('small', 'hud-caption', String(p.caption)));
    if (p.note) body.appendChild(el('p', 'hud-note hud-dim', String(p.note)));
    return body;
  }
  function panelBox(p, big) {
    var box = el('article', 'hud-panel hud-kind-' + (/^[a-z]+$/.test(p.kind || '') ? p.kind : 'markup') + (big ? ' is-big' : ''));
    box.setAttribute('data-accent', /^(amber|violet|green|red)$/.test(p.accent) ? p.accent : 'default');
    var head = el('div', 'hud-head'); head.appendChild(el('h3', 'hud-title', p.title || 'DISPLAY'));
    if (!big) {
      var x = el('button', 'hud-expand', '\u2922'); x.type = 'button'; x.title = 'Expand'; x.setAttribute('aria-label', 'Expand ' + (p.title || 'panel'));
      x.addEventListener('click', function () { openBlade(p); });
      head.appendChild(x);
    }
    box.appendChild(head);
    box.appendChild(panelBody(p, big));
    return box;
  }
  function renderPanels() {
    panelsEl.textContent = '';
    panelsEl.hidden = !act.panels.length;
    act.panels.slice(-4).reverse().forEach(function (p) { panelsEl.appendChild(panelBox(p, false)); });
    if (bladeId && !act.panels.some(function (p) { return p.id === bladeId; })) closeBlade();
  }
  /* the blade: one panel, large, over the ring (Esc or x closes) */
  var bladeEl = q('[data-jv-blade]'), bladeId = null;
  function openBlade(p) {
    if (!bladeEl) return;
    bladeId = p.id; bladeEl.textContent = '';
    var x = el('button', 'jv-blade-x', '\u00d7'); x.type = 'button'; x.setAttribute('aria-label', 'Close');
    x.addEventListener('click', closeBlade);
    bladeEl.appendChild(x); bladeEl.appendChild(panelBox(p, true));
    bladeEl.hidden = false; root.classList.add('has-blade');
  }
  function closeBlade() { if (!bladeEl) return; bladeId = null; bladeEl.hidden = true; bladeEl.textContent = ''; root.classList.remove('has-blade'); }
  function renderRail() { renderSteps(); renderTasks(); renderPanels(); }
  setInterval(function () {
    if (Object.keys(act.tasks).some(function (k) { return act.tasks[k].status === 'running'; })) renderTasks();
  }, 1000);

  /* ---- the live stream ---------------------------------------------------------------------- */
  var seq = null, es = null, turnText = {};
  function shouldSpeak(e) {
    if (cancelled[e.turn]) return false;
    if (myTurns[e.turn]) return true;
    return turnKinds[e.turn] === 'announce' && (e.origin === 'browser' || e.origin === 'any');
  }
  var handlers = {
    turn: function (e) {
      act.turns[e.turn] = { id: e.turn, heard: e.heard || '', kind: e.kind, origin: e.origin, steps: [], status: 'running', text: '' };
      act.order.push(e.turn); if (act.order.length > 8) delete act.turns[act.order.shift()];
      turnKinds[e.turn] = e.kind;
      if (e.kind === 'chat') act.panels = act.panels.filter(function (p) { return p.hold === 'sticky'; });
      if (e.kind === 'chat' && !myTurns[e.turn] && e.heard) youEl.textContent = e.heard;   // the laptop heard him
      turnText[e.turn] = '';
      working = '';
      renderRail();
    },
    sentence: function (e) {
      var t = act.turns[e.turn]; if (t) t.text = (t.text + ' ' + e.text).trim();
      if (cancelled[e.turn]) return;
      turnText[e.turn] = ((turnText[e.turn] || '') + ' ' + e.text).trim();
      caption(turnText[e.turn], true);
      spokeIn[e.turn] = 1;
      if (shouldSpeak(e)) speaker.say(e.text, e.turn);
      working = ''; renderSteps(); render();
    },
    tool: function (e) {
      var t = act.turns[e.turn]; if (!t) return;
      var s = null;
      t.steps.forEach(function (x) { if (x.id === e.tool_id) s = x; });
      if (s) s.label = e.label || s.label; else t.steps.push({ id: e.tool_id, label: e.label, status: 'running', parent: e.parent });
      if (!e.parent) working = e.label;
      if (myTurns[e.turn] && !spokeIn[e.turn] && !filled[e.turn] && !e.parent && !cancelled[e.turn]) {
        filled[e.turn] = 1; speaker.say(fillerFor(e.label || ''), e.turn, true);    // once per turn, then quiet
      }
      renderSteps(); render();
    },
    tool_done: function (e) {
      var t = act.turns[e.turn]; if (!t) return;
      t.steps.forEach(function (x) { if (x.id === e.tool_id) x.status = e.ok ? 'ok' : 'error'; });
      renderSteps();
    },
    done: function (e) {
      var t = act.turns[e.turn];
      if (t) { t.status = e.status === 'ok' ? 'ok' : e.status; t.steps.forEach(function (x) { if (x.status === 'running') x.status = 'ok'; }); }
      if (pending === e.turn) pending = null;
      if (e.text && !cancelled[e.turn] && (myTurns[e.turn] || turnKinds[e.turn] === 'announce')) {
        addHistory('them', e.text, turnKinds[e.turn] === 'announce' ? 'voice · task done' : 'voice · just now');
      }
      working = ''; renderSteps(); render();
    },
    error: function (e) { if (pending === e.turn) pending = null; fail(e.message || 'Something went wrong.'); },
    task: function (e) {
      var t = e.task || {};
      if (!t.id) return;
      act.tasks[t.id] = Object.assign(act.tasks[t.id] || {}, t);
      renderTasks();
    },
    panel: function (e) {
      if (!e.panel) return;
      act.panels.push(e.panel); renderPanels();
      if (e.panel.size === 'blade') openBlade(e.panel);
    },
    settings: function (e) {
      if (!e.voice) return;
      voiceCfg = e.voice; cachedVoice = undefined;
      if (always) { if (sttEngine() === 'browser') bsr.start(); else bsr.stop(); }
    },
    ui: function (e) { if (e.ui) applyUi(e.ui); if (e.effect) fireEffect(e.effect); },
    timing: function (e) { if (e.timing) diag.serverTiming = e.timing; },
    ignored: function () { if (!pending) youEl.textContent = ''; },
    brain: function (e) { brainState = e.state === 'reconnecting' ? 'reconnecting' : ''; render(); }
  };
  function connect() {
    if (!window.EventSource || !canTalk) return;
    if (es) es.close();
    es = new EventSource('/voice/api/stream' + (seq !== null ? '?after=' + seq : ''));
    Object.keys(handlers).forEach(function (type) {
      es.addEventListener(type, function (m) {
        var e; try { e = JSON.parse(m.data); } catch (x) { return; }
        if (typeof e.id === 'number') { if (seq !== null && e.id <= seq) return; seq = e.id; }
        handlers[type](e);
      });
    });
    es.addEventListener('hello', function (m) { try { var h = JSON.parse(m.data); if (seq === null) seq = h.seq; } catch (x) { /* ignore */ } });
    es.onerror = function () { /* EventSource reconnects by itself (retry: 2000) with Last-Event-ID */ };
  }
  function loadActivity() {
    return fetch('/voice/api/activity', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j) return;
        if (j.voice) voiceCfg = j.voice;
        (j.turns || []).forEach(function (t) {
          act.turns[t.id] = { id: t.id, heard: t.heard, kind: t.kind, origin: t.origin, steps: t.steps || [], status: t.status, text: t.text };
          act.order.push(t.id); turnKinds[t.id] = t.kind;
        });
        (j.tasks || []).forEach(function (t) { act.tasks[t.id] = t; });
        act.panels = j.panels || [];
        if (j.ui) applyUi(j.ui);
        seq = j.seq;
        renderRail();
      }).catch(function () { /* the stream still works */ });
  }

  /* ---- the ring: tap = on/off (and interrupts while speaking); hold Space = push-to-talk ------- */
  function postMute(muted) {     // the laptop's voice loop follows the ring (CSRF-checked)
    bg.muted = muted;
    return post('/voice/api/mute', { muted: muted ? '1' : '0' }).catch(function () { /* the tab state still holds */ });
  }
  function setOn(on) {
    dismissTip();
    if (on) {
      audioCtx();
      if (synth) synth.resume();     // unlock speech inside the click
      if (bg.online || bg.muted) postMute(false);
      ensureMic().then(function () {
        always = true; vad.armed = 0; vad.speaking = false; local = local === 'error' ? null : local;
        if (sttEngine() === 'browser') bsr.start();
        render();
      }, function (e) { micError(e); render(); });
    } else {
      always = false;
      bsr.stop();
      if (rec) stopRec(true);
      if (local === 'listening' || local === 'error') local = null;
      if (bg.online) postMute(true);
      releaseMic();
    }
    render();
  }
  ptt.addEventListener('click', function () {
    if (!canTalk) return;
    if (pttDown) return;                                        // Space on the focused ring is push-to-talk
    if (speaker.busy() || pending) { interrupt(); render(); return; }   // "Speaking — tap to interrupt"
    setOn(!isOn());
  });
  ptt.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  function press() {
    if (pttDown) return;
    pttDown = true; ptt.classList.add('is-down'); dismissTip();
    if (sttEngine() === 'browser') {      // hold to talk, transcribed in the browser
      audioCtx(); if (synth) synth.resume();
      speaker.cancel(); interrupt(); pttBrowser = true; bsr.start(); local = 'listening'; render(); return;
    }
    startRec('ptt');
  }
  function release() {
    if (!pttDown) return;
    setTimeout(function () { pttDown = false; }, 0);
    ptt.classList.remove('is-down');
    if (pttBrowser) {                     // the last words arrive a moment after the key comes up
      setTimeout(function () { bsr.flush(); pttBrowser = false; if (!always) bsr.stop(); }, 700);
      return;
    }
    speechEndAt = Date.now(); heardBy = 'server';
    if (rec && recMode === 'ptt') stopRec();
  }
  function typingIn(t) {
    if (!t || t === ptt) return false;
    return /^(INPUT|TEXTAREA|SELECT|BUTTON|A|SUMMARY)$/.test(t.tagName) || t.isContentEditable;
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { if (diagEl && !diagEl.hidden) toggleDiag(false); else if (bladeId) closeBlade(); else if (drawerOpen()) setDrawer(false); else if (menu && menu.open) menu.open = false; else if (rec && recMode === 'ptt') stopRec(true); else interrupt(); render(); return; }
    if (e.code !== 'Space' || e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target) || !canTalk) return;
    if (document.querySelector('dialog[open]')) return;
    e.preventDefault();
    if (!e.repeat) press();
  });
  document.addEventListener('keyup', function (e) { if (e.code === 'Space' && pttDown) { e.preventDefault(); release(); } });

  /* ---- "is it working?": D = diagnostics, T = audio self-test (src/ui/Diagnostics.tsx) ---------- */
  var diagEl = q('[data-jv-diag]'), diagTimer = null, diagServer = null, diagFetchAt = 0;
  function ago(t) { return t ? ((Date.now() - t) / 1000).toFixed(1) + 's ago' : '—'; }
  function micPermission() {
    if (!navigator.permissions || !navigator.permissions.query) { diag.micPerm = stream ? 'granted' : 'unknown'; return; }
    navigator.permissions.query({ name: 'microphone' }).then(function (r) { diag.micPerm = r.state; }, function () { diag.micPerm = stream ? 'granted' : 'unknown'; });
  }
  function diagRows() {
    var lvl = stream && micAnalyser ? Math.min(1, rms(micAnalyser) * 7) : -1, srv = diagServer || {}, b = srv.brain || {};
    var link = !es ? 'not connected' : es.readyState === 1 ? 'live' : es.readyState === 0 ? 'connecting' : 'closed';
    var lt = diag.lastTurn;
    return [
      ['Microphone', diag.micPerm + (stream ? ', open' : ', closed'), diag.micPerm === 'denied'],
      ['Mic level', lvl < 0 ? '—' : (Math.round(lvl * 100) + '%'), false, lvl],
      ['Hearing', sttLabel() + (sttEngine() === 'browser' ? (diag.sttRunning ? ', running' : ', idle') + ' · ' + diag.sessions + ' sessions, ' + diag.restarts + ' restarts' : ''), !!sttBroken],
      ['Last heard', diag.heard ? '"' + diag.heard.slice(-80) + '" · ' + ago(diag.heardAt) : (diag.interim ? '… ' + diag.interim.slice(-60) : '—')],
      ['STT error', diag.sttError || 'none', !!diag.sttError && diag.sttError !== 'no-speech' && diag.sttError !== 'aborted'],
      ['Brain link', link + (b.connected ? ' · session connected' : b.running ? ' · session starting' : ' · session idle') + (b.model ? ' · ' + b.model : ''), link !== 'live'],
      ['Last turn', lt ? 'speech end → first word ' + lt.ms + ' ms (' + lt.how + (lt.ask != null ? ', ask ' + lt.ask + ' ms' : '') + (lt.stt ? ', whisper ' + lt.stt + ' ms' : '') + ') · ' + ago(lt.at) : '—', lt && lt.ms > 4000],
      ['Voice', diag.tts || (voiceCfg.web_tts === 'browser' ? 'browser: ' + ((pickVoice() || {}).name || 'default voice') : voiceCfg.web_tts), diag.ttsFailures > 0],
      ['Last spoken', diag.spoken ? '"' + diag.spoken.slice(0, 70) + '" · ' + ago(diag.spokenAt) + (diag.ttsFailures ? ' · ' + diag.ttsFailures + ' failed' : '') : '—'],
      ['Self-test', diag.selfTest || 'press T', /^failed/.test(diag.selfTest)],
      ['MCP servers', (srv.mcp_servers || []).join(', ') || 'none']
    ];
  }
  function drawDiag() {
    if (!diagEl || diagEl.hidden) return;
    if (Date.now() - diagFetchAt > 3000) {
      diagFetchAt = Date.now();
      fetch('/voice/api/diagnostics', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (j) { if (j) diagServer = j; }).catch(function () { /* shown as link state */ });
      micPermission();
    }
    var list = diagEl.querySelector('[data-jv-diag-rows]');
    list.textContent = '';
    diagRows().forEach(function (r) {
      var row = el('div', 'diag-row' + (r[2] ? ' is-bad' : ''));
      row.appendChild(el('span', 'diag-k', r[0]));
      var v = el('span', 'diag-v', r[1]);
      if (r[3] != null && r[3] >= 0) { var bar = el('i', 'diag-bar'); bar.style.setProperty('--v', String(r[3])); v.appendChild(bar); }
      row.appendChild(v); list.appendChild(row);
    });
  }
  function toggleDiag(open) {
    if (!diagEl) return;
    var on = open == null ? diagEl.hidden : open;
    diagEl.hidden = !on;
    clearInterval(diagTimer);
    if (on) { diagFetchAt = 0; drawDiag(); diagTimer = setInterval(drawDiag, 300); }
  }
  function selfTest() {                  // speak one line; pass when playback actually starts
    audioCtx(); if (synth) synth.resume();
    var t0 = Date.now(), ok = false;
    diag.selfTest = 'running…';
    speaker.say('Audio check, ' + H + '. One, two, three.', null, true, function () {
      ok = true; diag.selfTest = 'passed: playback started in ' + (Date.now() - t0) + ' ms (' + (diag.tts || 'voice') + ')';
      flash('Audio self-test passed.');
    });
    setTimeout(function () {
      if (ok) return;
      diag.selfTest = 'failed: no playback within 3 s. Check the volume, the output device, and that this tab may play sound.';
      flash('Audio self-test failed: nothing played.');
    }, 3000);
    toggleDiag(true);
  }
  if (diagEl) { var dx = diagEl.querySelector('[data-jv-diag-x]'); if (dx) dx.addEventListener('click', function () { toggleDiag(false); }); }
  Array.prototype.forEach.call(document.querySelectorAll('[data-jv-diag-open]'), function (b) {
    b.addEventListener('click', function () { if (menu) menu.open = false; toggleDiag(); });
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-jv-selftest]'), function (b) {
    b.addEventListener('click', function () { if (menu) menu.open = false; selfTest(); });
  });
  document.addEventListener('keydown', function (e) {
    var tg = e.target, typing = tg && (/^(INPUT|TEXTAREA|SELECT)$/.test(tg.tagName) || tg.isContentEditable);
    if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || typing || document.querySelector('dialog[open]')) return;
    if (e.key === 'd' || e.key === 'D') { e.preventDefault(); toggleDiag(); }
    else if (e.key === 't' || e.key === 'T') { e.preventDefault(); selfTest(); }
  });

  /* ---- first visit: one dismissible tip ------------------------------------------------------ */
  var TIP_KEY = 'jv:tip-seen';
  function dismissTip() {
    if (!tip || tip.hidden) return;
    tip.hidden = true;
    try { localStorage.setItem(TIP_KEY, '1'); } catch (e) { /* private window: show again next time */ }
  }
  if (tip && canTalk) {
    var seen = null;
    try { seen = localStorage.getItem(TIP_KEY); } catch (e) { seen = null; }
    if (!seen) tip.hidden = false;
  }
  if (tipX) tipX.addEventListener('click', dismissTip);

  /* ---- history drawer ---------------------------------------------------------------------- */
  function drawerOpen() { return document.documentElement.classList.contains('jv-drawer-open'); }
  function setDrawer(open) {
    document.documentElement.classList.toggle('jv-drawer-open', open);
    if (drawer) drawer.inert = !open;
    if (scrim) scrim.hidden = !open;
    histBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && logEl) logEl.scrollTop = logEl.scrollHeight;
    if (!open) (menu ? menu.querySelector('summary') : histBtn).focus({ preventScroll: true });
  }
  if (drawer) drawer.inert = true;
  histBtn.addEventListener('click', function () { if (menu) menu.open = false; setDrawer(!drawerOpen()); });
  document.addEventListener('click', function (e) {     // close the "..." menu on an outside click
    if (menu && menu.open && !menu.contains(e.target)) menu.open = false;
  });
  if (scrim) scrim.addEventListener('click', function () { setDrawer(false); });
  var closeBtn = document.querySelector('[data-jv-close]');
  if (closeBtn) closeBtn.addEventListener('click', function () { setDrawer(false); });
  document.addEventListener('keydown', function (e) {   // "/" opens the drawer; home.js then focuses the box
    var t = e.target.tagName;
    if (e.key === '/' && t !== 'INPUT' && t !== 'TEXTAREA' && !e.metaKey && !e.ctrlKey && !drawerOpen()) setDrawer(true);
  });
  if (location.hash === '#history' || location.hash === '#talk') setDrawer(true);

  /* ---- the laptop's voice loop (its state pill; its words arrive on the stream) -------------- */
  function pollStatus() {
    if (document.hidden) return;
    fetch('/voice/api/status', { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; }).then(function (j) { if (j) { bg = j; render(); } })
      .catch(function () { /* offline: next tick */ });
  }
  pollStatus();
  setInterval(pollStatus, 4000);

  /* ---- the ui_* tools: accent, ring, rails, effects (adewaskar/jarvis bridge/ui.mjs, simplified) ---- */
  var uiCfg = { accent: null, ring: { style: 'arc', color: null, scale: 1, spin: 1 }, rails: true }, fxTimer = null;
  var HEX = /^#[0-9a-f]{6}$/i;
  function hexRgb(h) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }
  function applyUi(u) {
    var ring = u.ring || {};
    uiCfg = { accent: HEX.test(u.accent || '') ? u.accent : null,
      ring: { style: /^(arc|minimal|dense|radar)$/.test(ring.style || '') ? ring.style : 'arc', color: HEX.test(ring.color || '') ? ring.color : null,
        scale: Math.max(0.5, Math.min(1.5, +ring.scale || 1)), spin: Math.max(0, Math.min(4, ring.spin == null ? 1 : +ring.spin)) },
      rails: u.rails !== false };
    if (uiCfg.accent) root.style.setProperty('--jv', uiCfg.accent); else root.style.removeProperty('--jv');
    root.setAttribute('data-rails', uiCfg.rails ? '1' : '0');
    root.setAttribute('data-ring', uiCfg.ring.style);
  }
  function fireEffect(name) {
    if (!/^(pulse|scan|glitch|flash)$/.test(name) || reduced) return;
    ['pulse', 'scan', 'glitch', 'flash'].forEach(function (x) { root.classList.remove('fx-' + x); });
    void root.offsetWidth;                       // restart the animation
    root.classList.add('fx-' + name);
    clearTimeout(fxTimer); fxTimer = setTimeout(function () { root.classList.remove('fx-' + name); }, 1400);
  }

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
    var over = uiCfg.ring.color || uiCfg.accent, style = uiCfg.ring.style;
    var target = over && st !== 'error' ? hexRgb(over) : (COLORS[st] || COLORS.idle);
    for (var k = 0; k < 3; k++) col[k] += (target[k] - col[k]) * Math.min(1, dt * 4);
    var an = st === 'speaking' && src ? outAnalyser : (st === 'listening' && stream ? micAnalyser : null);
    var want = an ? Math.min(1, rms(an) * 7) : st === 'listening' ? 0.18 + 0.12 * Math.sin(now / 160) : st === 'speaking' ? 0.3 + 0.25 * Math.abs(Math.sin(now / 110)) : 0;
    level += (want - level) * Math.min(1, dt * 12);
    var speed = { idle: 0.06, listening: 0.18, thinking: 1.1, speaking: 0.25, muted: 0.015, error: 0.03 }[st] || 0.06;
    if (!reduced) spin += dt * speed * uiCfg.ring.spin;
    var w = canvas.width, cx = w / 2, cy = w / 2, R = w * 0.3 * (0.6 + 0.4 * uiCfg.ring.scale);
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

    var ticks = style === 'dense' ? 240 : 120, minimal = style === 'minimal';
    if (style === 'radar') {           // a sweeping beam
      var sw = spin * 1.6, beam = g.createRadialGradient(cx, cy, 0, cx, cy, R * 1.36);
      beam.addColorStop(0, rgba(col, 0.28 * dim)); beam.addColorStop(1, rgba(col, 0));
      g.fillStyle = beam; g.beginPath(); g.moveTo(cx, cy); g.arc(cx, cy, R * 1.36, sw, sw + 0.5); g.closePath(); g.fill();
    }
    // outer tick ring
    if (!minimal) {
    g.save(); g.translate(cx, cy); g.rotate(spin * 0.35);
    g.strokeStyle = rgba(col, 0.42 * dim); g.lineWidth = Math.max(1, w / 600);
    g.beginPath();
    for (var i = 0; i < ticks; i++) {
      var a = i / ticks * Math.PI * 2, r1 = R * 1.36, r2 = R * (i % 10 === 0 ? 1.43 : 1.39);
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
    }

    // audio-reactive spikes around the ring
    var spec = spectrum(an), bars = minimal ? 0 : style === 'dense' ? 192 : 96;
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
    var dots = minimal ? 60 : style === 'dense' ? 420 : 220;
    for (var p = 0; p < dots; p++) {
      var pa = p / dots * Math.PI * 2 + spin * 0.2, nz = noise(p, t);
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
  window.__jarvis = { ask: ask, sanitize: window.__sanitizePanel, diag: diag, stt: sttEngine, toggleDiag: toggleDiag,
    selfTest: selfTest, openBlade: openBlade, applyUi: applyUi };   // debugging / page tests
  loadActivity().then(connect, connect);
  if (synth) { pickVoice(); setTimeout(reportVoices, 1500); }
})();
