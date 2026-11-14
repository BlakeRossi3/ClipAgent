// Voice: home mic pill + mute toggle, Settings "test my mic" meter and microphone list (voice/web.py).
(function () {
  "use strict";
  var LABELS = { idle: "Listening", listening: "Listening to you", followup: "Listening", thinking: "Thinking",
    speaking: "Speaking", muted: "Muted", no_mic: "No microphone", starting: "Starting", offline: "Voice off", stopped: "Voice off" };
  var pill = document.querySelector("[data-voice-pill]");
  function poll() {
    fetch("/voice/api/status", { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); }).then(function (st) {
        if (!st.enabled && st.state === "offline") { pill.hidden = true; return; }
        pill.hidden = false;
        pill.dataset.state = st.state;
        pill.querySelector("[data-voice-label]").textContent = "Mic: " + (LABELS[st.state] || st.state);
        var b = pill.querySelector("[data-voice-mute]");
        b.textContent = st.muted ? "Unmute" : "Mute";
        b.dataset.muted = st.muted ? "1" : "0";
      }).catch(function () {});
  }
  if (pill) {
    pill.querySelector("[data-voice-mute]").addEventListener("click", function (e) {
      var body = new FormData();
      body.append("csrf", pill.dataset.csrf);
      body.append("muted", e.target.dataset.muted === "1" ? "0" : "1");
      fetch("/voice/api/mute", { method: "POST", body: body, credentials: "same-origin",
        headers: { Accept: "application/json" } }).then(poll);
    });
    poll();
    setInterval(poll, 3000);
  }
  // Settings: the Jarvis page's browser voice picker (the browser's own voices; British male first).
  var pick = document.querySelector("[data-web-voices]"), synth = window.speechSynthesis;
  function score(v) {
    var n = v.name.toLowerCase(), s = 0;
    if (/ryan.*(online|natural)/.test(n)) s += 130; else if (/thomas.*(online|natural)/.test(n)) s += 115;
    else if (/microsoft george/.test(n)) s += 95; else if (n.indexOf("google uk english male") >= 0) s += 90;
    else if (/^daniel/.test(n)) s += 88;
    if (/en[-_]gb/i.test(v.lang)) s += 25;
    return s;
  }
  function fillVoices() {
    if (!pick || !synth) return;
    var cur = pick.dataset.current || "", list = synth.getVoices().filter(function (v) { return /^en/i.test(v.lang); });
    if (!list.length) return;
    list.sort(function (a, b) { return score(b) - score(a) || a.name.localeCompare(b.name); });
    while (pick.options.length > 1) pick.remove(1);
    list.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v.name; o.textContent = v.name + (v.localService ? "" : " (online)");
      if (v.name === cur) o.selected = true;
      pick.appendChild(o);
    });
  }
  if (pick && synth) { fillVoices(); synth.addEventListener("voiceschanged", fillVoices); }
  var hear = document.querySelector("[data-web-voice-test]");
  if (hear && synth) hear.addEventListener("click", function () {
    synth.cancel(); synth.resume();
    var u = new SpeechSynthesisUtterance("Good evening, sir. This is how I will sound on the Jarvis page.");
    var name = pick && pick.value, all = synth.getVoices();
    var v = all.filter(function (x) { return x.name === name; })[0] ||
      all.filter(function (x) { return /^en/i.test(x.lang); }).sort(function (a, b) { return score(b) - score(a); })[0];
    if (v) { u.voice = v; u.lang = v.lang; }
    synth.speak(u);
  });
  var test = document.querySelector("[data-voice-test]");
  if (test) {
    var meter = document.querySelector("[data-voice-meter]"), note = document.querySelector("[data-voice-test-note]");
    var input = document.querySelector("[data-voice-devices]");
    fetch("/voice/api/devices", { headers: { Accept: "application/json" } }).then(function (r) { return r.json(); })
      .then(function (d) {
        var list = document.getElementById("voice-devs");
        (d.devices || []).forEach(function (x) { var o = document.createElement("option"); o.value = x.name; list.appendChild(o); });
        note.textContent = d.error ? d.error : (d.chosen ? "Automatic choice: " + d.chosen : "");
      }).catch(function () {});
    test.addEventListener("click", function () {
      var n = 0;
      (function once() {
        fetch("/voice/api/level?device=" + encodeURIComponent(input.value || ""), { headers: { Accept: "application/json" } })
          .then(function (r) { return r.json(); }).then(function (d) {
            meter.value = d.level || 0;
            note.textContent = d.error ? d.error : "Speak now - " + d.device;
            if (!d.error && ++n < 16) once(); else if (!d.error) note.textContent = "Done - " + d.device;
          });
      })();
    });
  }
})();
